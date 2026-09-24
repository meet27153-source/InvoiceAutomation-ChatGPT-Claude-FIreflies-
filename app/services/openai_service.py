"""ChatGPT subscription / OpenAI API invoice integration.

ChatGPT subscription billing and OpenAI API billing are separate systems.  This
adapter deliberately keeps the account type explicit and never attempts to
bypass MFA, CAPTCHA, SSO, or other provider security controls.
"""
import re
from urllib.parse import urljoin

from app.services.base import BaseService, InvoiceNotFoundError, ReauthRequiredError

LOGIN_URL = "https://auth.openai.com/log-in"
CHATGPT_HOME = "https://chatgpt.com/"
# The plain ChatGPT homepage is a chat UI with no billing content on it.
# Billing lives behind the Settings SPA route below -- navigating straight
# there (instead of opening the homepage and hoping something billing-related
# is discoverable) is what _open_chatgpt_billing() already does for the
# direct-Playwright path. run_invoice_cli() in chrome_auth.py opens its tab
# at billing_url directly with no further navigation, so billing_url itself
# must point at the actual Billing route for that CLI-attach workflow too.
CHATGPT_BILLING_URL = "https://chatgpt.com/#settings/Billing"
API_BILLING_URL = "https://platform.openai.com/settings/organization/billing/overview"


class OpenAIService(BaseService):
    slug = "openai"
    display_name = "OpenAI / ChatGPT"
    account_types = [
        ("chatgpt", "ChatGPT web subscription (Plus/Pro/Business)"),
        ("api", "OpenAI API / Platform"),
    ]

    @property
    def login_url(self):
        return LOGIN_URL

    @property
    def billing_url(self):
        return CHATGPT_BILLING_URL if self.account_type == "chatgpt" else API_BILLING_URL

    def _looks_logged_out(self) -> bool:
        url = self.page.url.lower()
        return any(x in url for x in ("/log-in", "/login", "/auth/"))

    def _assert_no_security_challenge(self):
        self.assert_not_blocked(
            r'verification code|two-factor|authenticator|check your email',
            'iframe[src*="captcha"]',
            "OpenAI",
        )

    def login(self) -> None:
        """Use the existing Chrome session; never automate Google credentials.

        If ChatGPT is not authenticated, the account is marked for manual
        re-authentication instead of trying to sign into Google from an
        automated Chromium profile.
        """
        page = self.page
        page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        self._assert_no_security_challenge()
        if self._looks_logged_out():
            raise ReauthRequiredError(
                "ChatGPT is not authenticated in the connected Chrome session. "
                "Use Authenticate / Re-authenticate and complete login manually."
            )

    @staticmethod
    def _click_text(page, labels, timeout=4000):
        for label in labels:
            selectors = [
                f'button:has-text("{label}")',
                f'a:has-text("{label}")',
                f'[role="button"]:has-text("{label}")',
                f'text={label}',
            ]
            for selector in selectors:
                loc = page.locator(selector).first
                if not loc.count():
                    continue
                try:
                    loc.click(timeout=timeout)
                    page.wait_for_timeout(700)
                    return True
                except Exception:
                    continue
        return False

    def _open_chatgpt_billing(self):
        """Open the current ChatGPT Settings -> Billing SPA route.

        The current ChatGPT UI exposes Billing as a Settings sub-route using
        the hash ``#settings/Billing``.  Going to that route directly is much
        more reliable than trying to discover a Settings/Billing button by
        visible text.  We still validate the rendered page before returning it.
        """
        page = self.page
        billing_route = "https://chatgpt.com/#settings/Billing"

        page.goto(billing_route, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        self._assert_no_security_challenge()

        if self._looks_logged_out():
            raise ReauthRequiredError(
                "ChatGPT session expired; authenticate again in your connected Chrome session."
            )

        # Hash navigation is handled by the ChatGPT SPA, so wait for the
        # actual Billing content instead of waiting for a second page load.
        try:
            page.get_by_text("Transaction history", exact=False).first.wait_for(
                state="visible", timeout=12000
            )
            return page
        except Exception:
            pass

        # Some accounts render the same section without the exact heading.
        # Inspect the URL and body before declaring failure.
        try:
            url = page.url.lower()
            body = page.locator("body").inner_text(timeout=8000).lower()
        except Exception:
            url, body = page.url.lower(), ""

        if "#settings/billing" in url and any(
            term in body for term in ("transaction history", "billing", "payment")
        ):
            return page

        raise InvoiceNotFoundError(
            "ChatGPT opened, but the current Settings → Billing route did not render "
            "Transaction history. The authenticated session may need re-authentication."
        )

    @staticmethod
    def _open_latest_transaction(page):
        """Open the newest transaction shown in ChatGPT Billing.

        The current UI shows a Transaction history section with a View all
        control and dated Paid rows.  We select the first (newest) dated row
        rather than relying on a generated CSS class or a guessed invoice URL.
        """
        try:
            view_all = page.get_by_role("button", name=re.compile(r"^view all$", re.I)).first
            if view_all.count() and view_all.is_visible():
                view_all.click(timeout=7000)
                page.wait_for_timeout(1200)
        except Exception:
            pass

        date_re = re.compile(r"\b\d{1,2}/\d{1,2}/20\d{2}\b")
        candidates = page.get_by_text(date_re)
        for idx in range(min(candidates.count(), 30)):
            candidate = candidates.nth(idx)
            try:
                if not candidate.is_visible():
                    continue
                text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
                if not date_re.search(text):
                    continue

                # The date is normally inside the clickable transaction row.
                # Try the date itself first, then its nearest clickable parent.
                for target in (
                    candidate,
                    candidate.locator("xpath=ancestor::*[@role='button'][1]"),
                    candidate.locator("xpath=ancestor::button[1]"),
                    candidate.locator("xpath=ancestor::a[1]"),
                    candidate.locator("xpath=.."),
                ):
                    try:
                        if target.count() and target.first.is_visible():
                            target.first.click(timeout=5000)
                            page.wait_for_timeout(1200)
                            return text
                    except Exception:
                        continue
            except Exception:
                continue

        return None

    @staticmethod
    def _invoice_download_links(page):
        """Return likely invoice/receipt download controls, strongest first."""
        selectors = [
            'a[href$=".pdf"]',
            'a[href*="invoice"]',
            'a[href*="receipt"]',
            'a:has-text("Download PDF")',
            'a:has-text("Download invoice")',
            'a:has-text("Download receipt")',
            'button:has-text("Download PDF")',
            'button:has-text("Download invoice")',
            'button:has-text("Download receipt")',
            'a:has-text("Download")',
            'button:has-text("Download")',
        ]
        seen = set()
        for selector in selectors:
            loc = page.locator(selector)
            for idx in range(loc.count()):
                item = loc.nth(idx)
                key = (item.get_attribute("href") or "") + "|" + (item.inner_text() or "")
                if key in seen:
                    continue
                seen.add(key)
                yield item

    @staticmethod
    def _extract_id_from_text(text: str):
        patterns = [
            re.compile(r"\b(in_[A-Za-z0-9_-]+)\b"),
            re.compile(r"\b(invoice(?:\s*(?:number|no\.?|id))?\s*[:#-]?\s*[A-Za-z0-9_-]{3,})\b", re.I),
            re.compile(r"\b(inv[-_ ]?[A-Za-z0-9_-]{3,})\b", re.I),
        ]
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return None

    def _extract_invoice(self, page) -> dict:
        """Identify the newest invoice without waiting for a hard-coded invoice row."""
        try:
            body = page.locator("body").inner_text(timeout=10000)
        except Exception as exc:
            raise InvoiceNotFoundError(f"Billing portal content could not be read: {type(exc).__name__}")

        lower = body.lower()
        if not any(term in lower for term in ("invoice", "receipt", "billing", "transaction history")):
            raise InvoiceNotFoundError(
                "The connected page is not the ChatGPT billing portal; no billing content was found."
            )

        # Current ChatGPT shows transaction history first. Open the newest
        # transaction so its invoice/receipt controls become available.
        transaction_text = self._open_latest_transaction(page)
        if transaction_text:
            try:
                body = page.locator("body").inner_text(timeout=10000)
                lower = body.lower()
            except Exception:
                pass

        # Prefer explicit invoice/receipt identifiers if the portal exposes them.
        external_id = self._extract_id_from_text(body)
        if external_id:
            return {"external_id": external_id, "date": self._extract_date(body), "raw_row_text": body[:2000]}

        if transaction_text:
            date = self._extract_date(transaction_text)
            fingerprint = re.sub(r"[^A-Za-z0-9]+", "-", transaction_text).strip("-")[:100]
            return {"external_id": "chatgpt-entry:" + fingerprint, "date": date, "raw_row_text": transaction_text}

        # If no provider ID is rendered, build a stable fingerprint from the
        # newest-looking billing entry. We never use a generic 'Invoice' string.
        rows = page.locator('tr, [role="row"], li, [class*="invoice"], [class*="Invoice"]')
        candidates = []
        for idx in range(min(rows.count(), 100)):
            text = re.sub(r"\s+", " ", (rows.nth(idx).inner_text() or "")).strip()
            if text and re.search(r"invoice|receipt|paid|\$|usd", text, re.I):
                candidates.append(text[:500])
        if candidates:
            text = candidates[0]
            date = self._extract_date(text)
            fingerprint = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:140]
            return {"external_id": "chatgpt-entry:" + fingerprint, "date": date, "raw_row_text": text}

        if next(self._invoice_download_links(page), None) is not None:
            # A download exists but no visible ID/row. Use the page URL + text
            # fingerprint; this still prevents duplicate sends for the same portal
            # entry while avoiding a fake invoice number.
            fingerprint = re.sub(r"[^A-Za-z0-9]+", "-", (page.url + body[-1000:])).strip("-")[:140]
            return {"external_id": "chatgpt-entry:" + fingerprint, "date": self._extract_date(body), "raw_row_text": body[-2000:]}

        raise InvoiceNotFoundError("No downloadable ChatGPT subscription invoice was found in the billing portal.")

    @staticmethod
    def _extract_date(text: str):
        patterns = [
            r"\b(20\d{2}-\d{2}-\d{2})\b",
            r"\b(\d{1,2}/\d{1,2}/20\d{2})\b",
            r"\b([A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    def get_latest_invoice(self) -> dict:
        if self.account_type == "chatgpt":
            page = self._open_chatgpt_billing()
        else:
            page = self.page
            page.goto(API_BILLING_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

        self._assert_no_security_challenge()
        if self._looks_logged_out():
            raise ReauthRequiredError("OpenAI session expired before billing could be opened.")
        return self._extract_invoice(page)

    def verify_billing_access(self) -> bool:
        """Used by the visible manual re-authentication flow."""
        if self.account_type == "chatgpt":
            portal = self._open_chatgpt_billing()
            text = portal.locator("body").inner_text(timeout=10000).lower()
            return "invoice" in text or "payment" in text or "billing" in text
        self.page.goto(API_BILLING_URL, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(1000)
        return not self._looks_logged_out()

    def download_invoice(self, invoice_info: dict, dest_path: str) -> str:
        page = self.page if self.account_type == "api" else self._open_chatgpt_billing()
        if self.account_type == "chatgpt":
            # Re-open the newest transaction because the billing page is an SPA
            # and the previous get_latest_invoice call may have returned before
            # the transaction detail was needed for the actual download.
            self._open_latest_transaction(page)
        links = list(self._invoice_download_links(page))
        if not links:
            raise InvoiceNotFoundError("Invoice was identified but no invoice download control was found.")

        wanted_id = invoice_info.get("external_id", "").lower()
        # Prefer a control whose surrounding text contains the identified invoice ID.
        ordered = sorted(
            links,
            key=lambda loc: 0 if wanted_id and wanted_id in ((loc.inner_text() or "").lower()) else 1,
        )
        for candidate in ordered:
            try:
                href = candidate.get_attribute("href") or ""
                if href and not href.startswith("javascript:"):
                    href = urljoin(page.url, href)
                    # Direct PDF links can be downloaded without relying on a
                    # brittle click target.
                    if ".pdf" in href.lower():
                        response = page.request.get(href)
                        if response.ok:
                            with open(dest_path, "wb") as f:
                                f.write(response.body())
                            return dest_path

                with page.expect_download(timeout=12000) as info:
                    candidate.click(timeout=5000)
                info.value.save_as(dest_path)
                return dest_path
            except Exception:
                continue

        raise InvoiceNotFoundError("Invoice was identified but no working PDF download control was found.")

