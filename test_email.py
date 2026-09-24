"""
test_email.py

Sends a test email using your already-configured SMTP settings, with a small
dummy PDF attached, to confirm email delivery works independently of the
invoice-download automation.

Usage:
    python test_email.py
"""
from app import db
from app.email_sender import send_invoice_email, EmailSendError


def main():
    settings = db.get_settings()

    print("Recipient emails:", settings.get("recipient_emails"))
    print("SMTP host:", settings.get("smtp_host"))
    print("SMTP port:", settings.get("smtp_port"))
    print("SMTP username:", settings.get("smtp_username"))
    print("Sender email:", settings.get("sender_email"))
    print()

    if not settings.get("recipient_emails"):
        print("No recipient emails configured. Set them in the web UI's Settings page first.")
        return
    if not settings.get("smtp_host"):
        print("No SMTP host configured. Set SMTP details in the web UI's Settings page first.")
        return

    dummy_pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF"
    )
    test_path = "test_invoice_dummy.pdf"
    with open(test_path, "wb") as f:
        f.write(dummy_pdf)

    try:
        send_invoice_email(
            settings,
            subject="[Test] Invoice Automation - SMTP test",
            body="This is a test email from test_email.py, confirming SMTP delivery works.",
            attachment_path=test_path,
            attachment_name="test_invoice.pdf",
        )
        print("SUCCESS: test email sent. Check the recipient inbox(es) above.")
    except EmailSendError as exc:
        print("FAILED to send email:")
        print(str(exc))


if __name__ == "__main__":
    main()