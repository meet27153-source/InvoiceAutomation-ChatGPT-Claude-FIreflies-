"""Common interface and safe helpers for subscription-service plugins."""
from abc import ABC, abstractmethod
import re


class ReauthRequiredError(Exception):
    """Raised when a valid session cannot be established without human login."""


class InvoiceNotFoundError(Exception):
    """Raised when the billing area contains no usable invoice."""


class BaseService(ABC):
    slug = "base"
    display_name = "Base Service"
    account_types = [("default", "Default")]

    def __init__(self, page, username: str, password: str, session_state: dict | None = None, account_type: str = "default"):
        self.page = page
        self.username = username
        self.password = password
        self.session_state = session_state
        self.account_type = account_type or "default"

    @property
    @abstractmethod
    def login_url(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def billing_url(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def login(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_invoice(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def download_invoice(self, invoice_info: dict, dest_path: str) -> str:
        raise NotImplementedError

    def get_storage_state(self) -> dict:
        return self.page.context.storage_state()

    def assert_not_blocked(self, mfa_pattern: str, captcha_css: str, provider: str):
        # Keep text matching and CSS matching separate. Playwright's locator()
        # parses CSS; regex text belongs in get_by_text().
        if self.page.get_by_text(re.compile(mfa_pattern, re.IGNORECASE)).count() > 0:
            raise ReauthRequiredError(f"{provider} requires MFA/verification; complete re-authentication manually.")
        if self.page.locator(captcha_css).count() > 0:
            raise ReauthRequiredError(f"{provider} is showing a CAPTCHA; complete re-authentication manually.")
        if self.page.get_by_text(re.compile(r"verify you are human|captcha", re.IGNORECASE)).count() > 0:
            raise ReauthRequiredError(f"{provider} is showing a CAPTCHA; complete re-authentication manually.")
