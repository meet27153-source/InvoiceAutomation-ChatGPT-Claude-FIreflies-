"""SMTP delivery of invoice PDFs with bounded retries."""
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path
from app.config import MAX_RETRIES, RETRY_BACKOFF_SECONDS
from app.crypto import decrypt
from app.logging_setup import log


class EmailSendError(Exception):
    pass


def _validate_settings(settings):
    recipients = settings.get("recipient_emails") or []
    if not recipients:
        raise EmailSendError("No recipient email addresses configured.")
    if not settings.get("smtp_host"):
        raise EmailSendError("SMTP host is not configured.")
    if not settings.get("smtp_username"):
        raise EmailSendError("SMTP username is not configured.")
    if not settings.get("encrypted_smtp_password"):
        raise EmailSendError("SMTP password/app password is not configured.")
    if not settings.get("sender_email"):
        raise EmailSendError("Sender email is not configured.")
    try:
        port = int(settings.get("smtp_port") or 587)
    except (TypeError, ValueError) as exc:
        raise EmailSendError("SMTP port is invalid.") from exc
    if not 1 <= port <= 65535:
        raise EmailSendError("SMTP port is outside the valid range.")
    return recipients, port


def _build_message(settings, subject, body, attachment_path=None, attachment_name=None):
    recipients, _ = _validate_settings(settings)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings["sender_email"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if attachment_path:
        path = Path(attachment_path)
        if not path.is_file():
            raise EmailSendError(f"Invoice attachment does not exist: {path.name}")
        data = path.read_bytes()
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=attachment_name or path.name)
    return msg


def send_invoice_email(settings, subject, body, attachment_path, attachment_name):
    recipients, port = _validate_settings(settings)
    smtp_password = decrypt(settings.get("encrypted_smtp_password") or "")
    msg = _build_message(settings, subject, body, attachment_path, attachment_name)
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(settings["smtp_host"], port, timeout=30) as server:
                if settings.get("smtp_use_tls", True):
                    server.starttls()
                server.login(settings["smtp_username"], smtp_password)
                server.send_message(msg)
            log.info("Invoice email sent to %d recipient(s) on attempt %d.", len(recipients), attempt)
            return
        except Exception as exc:
            last_error = exc
            log.warning("Email send attempt %d/%d failed: %s", attempt, MAX_RETRIES, type(exc).__name__)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise EmailSendError(f"Failed to send invoice email after {MAX_RETRIES} attempts: {type(last_error).__name__}")


def send_test_email(settings):
    """Send a small configuration test without an attachment."""
    recipients, port = _validate_settings(settings)
    smtp_password = decrypt(settings.get("encrypted_smtp_password") or "")
    msg = _build_message(settings, "Invoice Automation - SMTP test", "SMTP configuration test succeeded.")
    try:
        with smtplib.SMTP(settings["smtp_host"], port, timeout=30) as server:
            if settings.get("smtp_use_tls", True):
                server.starttls()
            server.login(settings["smtp_username"], smtp_password)
            server.send_message(msg)
    except Exception as exc:
        raise EmailSendError(f"SMTP test failed: {type(exc).__name__}") from exc
    log.info("SMTP test email sent to %d recipient(s).", len(recipients))
