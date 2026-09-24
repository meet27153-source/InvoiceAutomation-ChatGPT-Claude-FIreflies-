"""End-to-end invoice pipeline with per-account isolation and retries."""
from __future__ import annotations

import json
import os
import threading
import time

from playwright.sync_api import sync_playwright

from app import db
from app.config import INVOICE_DIR, MAX_RETRIES, RETRY_BACKOFF_SECONDS
from app.crypto import decrypt, encrypt
from app.email_sender import EmailSendError, send_invoice_email
from app.logging_setup import log
from app.services import get_service_class
from app.services.base import InvoiceNotFoundError, ReauthRequiredError

_RUN_LOCK = threading.Lock()

_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _settings_with_account_email(settings: dict, account: dict) -> dict:
    """
    Returns a copy of settings whose recipient_emails also includes the
    account's own login email (the address you're signed into that AI service
    with), in addition to whatever accountant/finance addresses are configured
    in Settings. De-duplicated, case-insensitive.
    """
    merged = list(settings.get("recipient_emails") or [])
    username = (account.get("username") or "").strip()
    if _EMAIL_RE.match(username):
        if username.lower() not in {r.lower() for r in merged}:
            merged.append(username)
    patched = dict(settings)
    patched["recipient_emails"] = merged
    return patched


def _run_with_retry(func, what: str, max_retries: int = MAX_RETRIES):
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except (ReauthRequiredError, InvoiceNotFoundError):
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("%s attempt %d/%d failed: %s", what, attempt, max_retries, type(exc).__name__)
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise last_exc



def _uses_attached_chrome(account: dict, service_cls) -> bool:
    """Services whose billing pages must be driven in the user's real Chrome.

    ChatGPT always; any other service that sets `uses_chrome_cli = True`
    (Claude: passwordless login + Cloudflare make a headless replay of saved
    cookies unreliable).
    """
    if account["service"] == "openai" and account.get("account_type") == "chatgpt":
        return True
    return bool(getattr(service_cls, "uses_chrome_cli", False))


def _run_via_attached_chrome(account: dict, dest_path: str) -> dict:
    """Run the invoice workflow in the user's already-open Chrome.

    Chrome 136+ blocks the old default-profile CDP trick. Playwright's official
    extension attachment is specifically intended for existing tabs/sessions,
    so this path deliberately does not launch a second/headless Chromium.
    """
    from app.chrome_auth import run_invoice_cli
    return run_invoice_cli(account, dest_path)


def process_account(account: dict, settings: dict) -> dict:
    account_id = account["id"]
    label = account["label"]
    run_id = db.start_run(account_id, label)
    log.info("Starting run for account '%s' (%s).", label, account["service"])

    browser = None
    context = None
    try:
        service_cls = get_service_class(account["service"])

        # ChatGPT (and Claude) must use the user's real Chrome session. Do not silently
        # replace it with a fresh headless browser: the current ChatGPT billing
        # UI and authenticated session are tied to the real browser context.
        if _uses_attached_chrome(account, service_cls):
            svc = account["service"]
            dest_path = os.path.join(INVOICE_DIR, f"{svc}_{account_id}_pending.pdf")
            invoice_info = _run_via_attached_chrome(account, dest_path)
            external_id = invoice_info["external_id"]

            if db.is_invoice_processed(account_id, external_id):
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except OSError:
                    pass
                db.set_account_needs_reauth(account_id, False)
                db.finish_run(run_id, "no_new_invoice", f"Invoice {external_id} was already emailed.")
                return {"status": "no_new_invoice", "account": label, "invoice": external_id}

            safe_ext = external_id.replace("/", "_").replace("\\", "_").replace(":", "_")
            final_path = os.path.join(INVOICE_DIR, f"{svc}_{account_id}_{safe_ext}.pdf")
            if dest_path != final_path and os.path.exists(dest_path):
                os.replace(dest_path, final_path)

            subject = f"[Invoice] {service_cls.display_name} - {label} - {external_id}"
            body = (
                "Automated invoice delivery.\n\n"
                f"Service: {service_cls.display_name}\n"
                f"Account: {label}\n"
                f"Invoice: {external_id}\n"
                f"Date: {invoice_info.get('date') or 'unknown'}\n"
            )
            _run_with_retry(
                lambda: send_invoice_email(
                    _settings_with_account_email(settings, account),
                    subject, body, final_path, os.path.basename(final_path),
                ),
                "Email delivery",
            )
            inserted = db.mark_invoice_processed(account_id, external_id, invoice_info.get("date"))
            db.set_account_needs_reauth(account_id, False)
            db.finish_run(run_id, "success", f"Invoice {external_id} downloaded and emailed.")
            return {"status": "success" if inserted else "no_new_invoice", "account": label, "invoice": external_id}
        password = decrypt(account.get("encrypted_password") or "")

        session_state = None
        if account.get("encrypted_session"):
            try:
                session_state = json.loads(decrypt(account["encrypted_session"]))
            except Exception:
                log.warning(
                    "Stored session for '%s' could not be decrypted; authentication is required again.",
                    label,
                )

        # Unattended runs use the encrypted session captured during manual
        # authentication in the user's normal Chrome. This means scheduled runs
        # do not depend on the everyday Chrome window remaining open.
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(storage_state=session_state, accept_downloads=True)
            page = context.new_page()
            service = service_cls(
                page,
                account["username"],
                password,
                session_state,
                account.get("account_type", "default"),
            )

            service.login()
            invoice_info = service.get_latest_invoice()
            external_id = invoice_info["external_id"]

            if db.is_invoice_processed(account_id, external_id):
                try:
                    db.update_account_session(account_id, encrypt(json.dumps(service.get_storage_state())))
                    db.set_account_needs_reauth(account_id, False)
                except Exception:
                    pass
                db.finish_run(run_id, "no_new_invoice", f"Invoice {external_id} was already emailed.")
                log.info("No new invoice for '%s'.", label)
                return {"status": "no_new_invoice", "account": label, "invoice": external_id}

            safe_id = external_id.replace("/", "_").replace("\\", "_")
            dest_path = os.path.join(
                INVOICE_DIR,
                f"{account['service']}_{account_id}_{safe_id}.pdf",
            )

            _run_with_retry(
                lambda: service.download_invoice(invoice_info, dest_path),
                "Invoice download",
            )

            subject = f"[Invoice] {service_cls.display_name} - {label} - {external_id}"
            body = (
                "Automated invoice delivery.\n\n"
                f"Service: {service_cls.display_name}\n"
                f"Account: {label}\n"
                f"Invoice: {external_id}\n"
                f"Date: {invoice_info.get('date') or 'unknown'}\n"
            )

            _run_with_retry(
                lambda: send_invoice_email(
                    _settings_with_account_email(settings, account),
                    subject,
                    body,
                    dest_path,
                    os.path.basename(dest_path),
                ),
                "Email delivery",
            )

            inserted = db.mark_invoice_processed(
                account_id,
                external_id,
                invoice_info.get("date"),
            )
            if not inserted:
                log.warning(
                    "Invoice %s for '%s' was already marked processed; a duplicate email may have occurred.",
                    external_id,
                    label,
                )

            try:
                new_state = service.get_storage_state()
                db.update_account_session(account_id, encrypt(json.dumps(new_state)))
            except Exception:
                log.warning("Could not persist refreshed session for '%s' (non-fatal).", label)

            db.set_account_needs_reauth(account_id, False)
            db.finish_run(run_id, "success", f"Invoice {external_id} downloaded and emailed.")
            log.info("Run succeeded for '%s': invoice %s emailed.", label, external_id)
            return {"status": "success", "account": label, "invoice": external_id}

    except ReauthRequiredError as exc:
        db.set_account_needs_reauth(account_id, True)
        db.finish_run(run_id, "needs_reauth", str(exc))
        log.error("Account '%s' needs re-authentication: %s", label, exc)
        return {"status": "needs_reauth", "account": label, "detail": str(exc)}
    except InvoiceNotFoundError as exc:
        db.finish_run(run_id, "failed", str(exc))
        log.error("Invoice retrieval failed for '%s': %s", label, exc)
        return {"status": "failed", "account": label, "detail": str(exc)}
    except EmailSendError as exc:
        db.finish_run(run_id, "failed", f"Downloaded but email failed: {exc}")
        log.error("Email delivery failed for '%s': %s", label, exc)
        return {"status": "failed", "account": label, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error processing account '%s'.", label)
        db.finish_run(run_id, "failed", f"Unexpected error: {type(exc).__name__}: {exc}")
        return {"status": "failed", "account": label, "detail": str(exc)}
    finally:
        # Playwright-created browser/context only. The user's normal Chrome is
        # never touched by scheduled runs.
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def run_all() -> list:
    """Run each enabled account independently; used by both scheduler and UI."""
    if not _RUN_LOCK.acquire(blocking=False):
        log.warning("A run is already in progress; this trigger was skipped.")
        return [{"status": "skipped", "detail": "Another automation run is already in progress."}]

    try:
        settings = db.get_settings()
        accounts = db.list_accounts(enabled_only=True)
        log.info("Starting invoice run for %d enabled account(s).", len(accounts))
        results = [process_account(account, settings) for account in accounts]
        log.info("Run complete: %s", json.dumps(results))
        return results
    finally:
        _RUN_LOCK.release()