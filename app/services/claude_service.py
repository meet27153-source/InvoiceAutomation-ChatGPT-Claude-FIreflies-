"""Anthropic / Claude billing plugin.

Claude.ai subscriptions and Anthropic Console billing are separate. The account
setup UI therefore requires the user to choose which billing system is relevant.

Claude.ai login is passwordless (email link / Google SSO), so it cannot be
automated here. Authenticate through the attached-Chrome flow in chrome_auth.py.

Invoices are Stripe-hosted. Depending on the page, a row exposes either:
  * an <a href="invoice.stripe.com/..."> link, or
  * a bare "View" button (no href) that opens the Stripe page via window.open.
Both are handled; the button case is resolved by clicking and capturing the
popup (or same-tab navigation) URL.
"""
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from app.services.base import BaseService, ReauthRequiredError, InvoiceNotFoundError

CLAUDE_LOGIN_URL = "https://claude.ai/login"
CLAUDE_BILLING_URL = "https://claude.ai/settings/billing"
CONSOLE_LOGIN_URL = "https://console.anthropic.com/login"
CONSOLE_BILLING_URL = "https://console.anthropic.com/settings/billing"

# Per-invoice Stripe hosts only. billing.stripe.com is the customer portal:
# its URLs are per-session (not per-invoice) and a "Manage billing" link to it
# would be mis-detected as the latest invoice, so it is deliberately excluded.
STABLE_STRIPE_HOSTS = ("invoice.stripe.com", "pay.stripe.com")
STRIPE_LINK = 'a[href*="invoice.stripe.com"], a[href*="pay.stripe.com"]'
VIEW_NAME = re.compile(r"^\s*view(?:\s+(?:invoice|receipt))?\s*$", re.I)

BILLING_WAIT_S = 15
DATE_RE = re.compile(r"\b(?:[A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2})\b")

# Distinct failure causes that all look like "no invoice link" from outside.
NO_INVOICE_HINTS = (
    (re.compile(r"verify you are human|checking your browser|just a moment", re.I),
     "a Cloudflare bot check blocked the automated browser; use the attached-Chrome (CLI) path"),
    (re.compile(r"app store|apple id|in-app purchase", re.I),
     "the subscription looks billed through Apple; Stripe has no invoices for it"),
    (re.compile(r"google play", re.I),
     "the subscription looks billed through Google Play; Stripe has no invoices for it"),
    (re.compile(r"contact your (?:admin|owner)|managed by your (?:organization|admin)", re.I),
     "billing is managed by the organization owner; this account cannot see invoices"),
    (re.compile(r"no invoices|no billing history|no payments", re.I),
     "the account has no invoices yet"),
)

# Walk up from the control to the nearest ancestor that contains a date:
# that is the invoice row (works for <tr> tables and div-based grids alike).
ROW_TEXT_JS = r"""el => {
  const re = /\b(?:[A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2}|\d{1,2}\/\d{1,2}\/20\d{2})\b/;
  for (let n = el; n && n !== document.body; n = n.parentElement) {
    const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
    if (re.test(t)) return t.slice(0, 500);
  }
  return (el.innerText || '').trim();
}"""


def _parse_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(m.group(0), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _stripe_id(href):
    """Last path segment of a per-invoice Stripe URL; None for anything else."""
    u = urlparse(href or "")
    if u.hostname not in STABLE_STRIPE_HOSTS:
        return None
    seg = u.path.rstrip("/").split("/")[-1]
    if seg == "pdf":  # .../<token>/pdf -> use the token, not "pdf"
        parts = u.path.rstrip("/").split("/")
        seg = parts[-2] if len(parts) > 1 else ""
    return f"stripe:{seg}" if seg else None


def _row_id(row_text):
    return "row:" + re.sub(r"\W+", "-", row_text or "").strip("-")[:80]


class ClaudeService(BaseService):
    slug = "claude"
    display_name = "Claude (Anthropic)"
    account_types = [
        ("claude_ai", "Claude.ai Pro/Team subscription"),
        ("console", "Anthropic API / Console"),
    ]
    # Dispatcher hint: route this service through chrome_auth.run_invoice_cli.
    # NOTE: if you are seeing errors raised from *this* file, the dispatcher is
    # not honouring this flag and is running the Python path instead.
    uses_chrome_cli = True

    @property
    def login_url(self):
        return CLAUDE_LOGIN_URL if self.account_type == "claude_ai" else CONSOLE_LOGIN_URL

    @property
    def billing_url(self):
        return CLAUDE_BILLING_URL if self.account_type == "claude_ai" else CONSOLE_BILLING_URL

    def login(self):
        page = self.page
        page.goto(self.billing_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if "login" in page.url.lower():
            raise ReauthRequiredError(
                "Claude uses passwordless/SSO login; run Authenticate / Re-authenticate in Chrome."
            )

    # ---- billing page -----------------------------------------------------

    @staticmethod
    def _view_control(page):
        """First visible-in-DOM 'View' button or link, or None."""
        for loc in (page.get_by_role("button", name=VIEW_NAME),
                    page.get_by_role("link", name=VIEW_NAME)):
            if loc.count():
                return loc.first
        return None

    @staticmethod
    def _diagnose(page):
        try:
            text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=5000))
        except Exception:
            text = ""
        for rx, reason in NO_INVOICE_HINTS:
            if rx.search(text):
                return f"No invoices on {page.url}: {reason}."
        return (
            f"No Stripe invoice link or 'View' control found on {page.url}. "
            f"Page text (last 600 chars): {text[-600:]!r}"
        )

    def _open_billing(self):
        """Return (page, kind) where kind is 'link' or 'button'."""
        page = self.page
        page.goto(self.billing_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if "login" in page.url.lower():
            raise ReauthRequiredError("Claude session expired before billing could be opened.")

        links = page.locator(STRIPE_LINK)
        deadline = time.monotonic() + BILLING_WAIT_S
        while time.monotonic() < deadline:
            if links.count():
                return page, "link"
            if self._view_control(page) is not None:
                return page, "button"
            page.wait_for_timeout(500)
        raise InvoiceNotFoundError(self._diagnose(page))

    def _resolve_click_target(self, page, control):
        """Click a JS 'View' control and return the Stripe URL it opens."""
        start_url = page.url
        popup = None
        try:
            with page.context.expect_page(timeout=15000) as info:
                control.click(timeout=5000)
            popup = info.value
        except Exception:
            popup = None  # no new tab: maybe same-tab navigation, checked below

        if popup is not None:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
                if popup.url in ("", "about:blank"):  # window.open() then navigate
                    try:
                        popup.wait_for_url(re.compile(r"^https?://"), timeout=10000)
                    except Exception:
                        pass
                url = popup.url
            finally:
                popup.close()
        else:
            page.wait_for_timeout(2000)
            url = page.url if page.url != start_url else ""

        if not url or "stripe.com" not in (urlparse(url).hostname or ""):
            raise InvoiceNotFoundError(
                f"Clicked 'View' on {start_url} but it did not open a Stripe invoice "
                f"(landed on {url or 'nothing'})."
            )
        return url

    def get_latest_invoice(self):
        page, kind = self._open_billing()
        if kind == "link":
            control = page.locator(STRIPE_LINK).first
            href = control.get_attribute("href") or ""
            row_text = control.evaluate(ROW_TEXT_JS)
        else:
            control = self._view_control(page)
            row_text = control.evaluate(ROW_TEXT_JS)  # read before clicking
            href = self._resolve_click_target(page, control)
        return {
            "external_id": _stripe_id(href) or _row_id(row_text),
            "date": _parse_date(row_text),
            "raw_row_text": row_text,
            "href": href,
        }

    # ---- download ---------------------------------------------------------

    @staticmethod
    def _write_if_pdf(resp, dest_path):
        if resp is not None and resp.ok:
            body = resp.body()
            if body[:4] == b"%PDF":
                with open(dest_path, "wb") as f:
                    f.write(body)
                return True
        return False

    def download_invoice(self, invoice_info, dest_path):
        href = invoice_info.get("href")
        if not href:
            raise InvoiceNotFoundError("Invoice has no Stripe link to download from.")

        request = self.page.context.request

        # 0) The View popup sometimes lands directly on the PDF URL.
        try:
            if self._write_if_pdf(request.get(href), dest_path):
                return dest_path
        except Exception:
            pass

        stripe = self.page.context.new_page()
        try:
            try:
                stripe.goto(href, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass  # e.g. "Download is starting"; the locators below still apply

            # 1) Stripe's "Download invoice" is usually an <a> to a /pdf URL.
            pdf_link = stripe.locator('a[href*="/pdf"]').first
            try:
                pdf_link.wait_for(state="attached", timeout=10000)
                if self._write_if_pdf(request.get(pdf_link.get_attribute("href")), dest_path):
                    return dest_path
            except Exception:
                pass

            # 2) Click the button and catch the browser download.
            pattern = re.compile(r"download (invoice|receipt)", re.I)
            for loc in (stripe.get_by_role("button", name=pattern),
                        stripe.get_by_role("link", name=pattern),
                        stripe.get_by_text(pattern)):
                try:
                    loc.first.wait_for(state="visible", timeout=8000)
                    with stripe.expect_download(timeout=20000) as dl:
                        loc.first.click(timeout=5000)
                    dl.value.save_as(dest_path)
                    return dest_path
                except Exception:
                    continue
        finally:
            stripe.close()
        raise InvoiceNotFoundError(
            f"Opened the Stripe invoice page ({href}) but could not download the PDF."
        )