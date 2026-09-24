"""Fireflies.ai billing plugin.

The provider may change its billing UI. This plugin deliberately uses semantic
fallbacks and never bypasses MFA/CAPTCHA. Verify selectors against the account
before relying on unattended production runs.
"""
import re
from app.services.base import BaseService, ReauthRequiredError, InvoiceNotFoundError

LOGIN_URL = "https://app.fireflies.ai/login"
BILLING_URL = "https://app.fireflies.ai/settings/billing"


class FirefliesService(BaseService):
    slug = "fireflies"
    display_name = "Fireflies"
    account_types = [("default", "Fireflies account")]

    @property
    def login_url(self):
        return LOGIN_URL

    @property
    def billing_url(self):
        return BILLING_URL

    def login(self):
        page = self.page
        page.goto(BILLING_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        if "login" not in page.url.lower():
            return
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        email = page.locator('input[type="email"], input[name="email"]').first
        password = page.locator('input[type="password"], input[name="password"]').first
        if email.count() == 0 or password.count() == 0 or not self.password:
            raise ReauthRequiredError("Fireflies login form requires manual authentication or has changed.")
        email.fill(self.username)
        password.fill(self.password)
        page.locator('button[type="submit"], button:has-text("Log in"), button:has-text("Sign in")').first.click()
        page.wait_for_timeout(2000)
        self.assert_not_blocked(
            r'verification code|two-factor|authenticator|check your email',
            'iframe[src*="captcha"]',
            "Fireflies",
        )
        if "login" in page.url.lower():
            raise ReauthRequiredError("Fireflies login did not complete; manual re-authentication required.")

    def get_latest_invoice(self):
        page = self.page
        page.goto(BILLING_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if "login" in page.url.lower():
            raise ReauthRequiredError("Fireflies session expired before billing could be opened.")
        body = page.locator("body").inner_text()
        for line in (x.strip() for x in body.splitlines() if x.strip()):
            match = re.search(r"\b(in_[A-Za-z0-9_-]+|INV[-_ ]?[A-Za-z0-9_-]{3,})\b", line, re.I)
            if match:
                return {"external_id": match.group(1), "date": None, "raw_row_text": line}
        links = page.locator('a:has-text("Invoice"), a:has-text("Receipt"), a:has-text("Download"), a[href*="invoice"], a[href*="receipt"]')
        if links.count() == 0:
            raise InvoiceNotFoundError("No Fireflies invoice/receipt control was found on the billing page.")
        text = (links.first.inner_text() or "invoice").strip()
        return {"external_id": "page:" + re.sub(r"\s+", " ", text)[:80], "date": None, "raw_row_text": text}

    def download_invoice(self, invoice_info, dest_path):
        page = self.page
        links = page.locator('a[href*="invoice"], a[href*="receipt"], a:has-text("Download"), a:has-text("Invoice"), a:has-text("Receipt")')
        for i in range(links.count()):
            link = links.nth(i)
            try:
                with page.expect_download(timeout=10000) as info:
                    link.click(timeout=5000)
                info.value.save_as(dest_path)
                return dest_path
            except Exception:
                continue
        raise InvoiceNotFoundError("Fireflies invoice was identified but no PDF download control worked.")
