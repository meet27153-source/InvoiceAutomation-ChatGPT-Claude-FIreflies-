"""Local Flask configuration and monitoring UI."""
import json
import threading

from flask import Blueprint, Flask, flash, jsonify, redirect, render_template, request, url_for

from app import db, scheduler
from app.config import WEB_SECRET_KEY_PATH
from app.crypto import encrypt
from app.logging_setup import log
from app.orchestrator import run_all
from app.services import SERVICE_REGISTRY

bp = Blueprint("main", __name__)

# Background manual-auth state per account id:
#   {"state": "pending"} while waiting for login,
#   {"state": "failed", "error": "..."} if it failed (shown once, then cleared).
# Successful auths are simply removed; the DB flag is the source of truth.
_AUTH_STATE = {}
_AUTH_LOCK = threading.Lock()


def _flash_auth_failures():
    """Show background auth failures once, on the next page load."""
    with _AUTH_LOCK:
        failed = {aid: s for aid, s in _AUTH_STATE.items() if s["state"] == "failed"}
        for aid in failed:
            _AUTH_STATE.pop(aid, None)
    for aid, s in failed.items():
        flash(f"Authentication failed for account {aid}: {s['error']}", "error")


def _valid_email(value):
    import re
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _validate_schedule(schedule_type, schedule_value):
    if schedule_type not in {"days", "monthly", "cron"}:
        raise ValueError("Invalid schedule type.")
    if schedule_type == "days":
        n = int(schedule_value)
        if n < 1:
            raise ValueError("Every N days must be at least 1.")
    elif schedule_type == "monthly":
        day = int(schedule_value)
        if not 1 <= day <= 28:
            raise ValueError("Monthly day must be between 1 and 28.")
    elif schedule_type == "cron":
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(schedule_value)


@bp.route("/")
def dashboard():
    _flash_auth_failures()
    return render_template("index.html", accounts=db.list_accounts(), runs=db.list_recent_runs(limit=10), settings=db.get_settings())


@bp.route("/accounts")
def accounts():
    _flash_auth_failures()
    return render_template("accounts.html", accounts=db.list_accounts(), services=SERVICE_REGISTRY)


@bp.route("/api/auth-status")
def auth_status():
    """Polled by the accounts/dashboard pages while an authentication is pending."""
    with _AUTH_LOCK:
        pending = [aid for aid, s in _AUTH_STATE.items() if s["state"] == "pending"]
        failed = [aid for aid, s in _AUTH_STATE.items() if s["state"] == "failed"]
    return jsonify({
        "pending": pending,
        "failed": failed,
        "needs_reauth": {str(a["id"]): bool(a["needs_reauth"]) for a in db.list_accounts()},
    })


@bp.route("/accounts/add", methods=["GET", "POST"])
def add_account():
    if request.method == "POST":
        service = request.form.get("service", "").strip()
        account_type = request.form.get("account_type", "default").strip()
        label = request.form.get("label", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        cls = SERVICE_REGISTRY.get(service)
        valid_types = {slug for slug, _ in (cls.account_types if cls else [])}
        if not cls or account_type not in valid_types:
            flash("Invalid service/account type.", "error")
            return redirect(url_for("main.add_account"))
        if not (label and username):
            flash("Label and username/email are required.", "error")
            return redirect(url_for("main.add_account"))
        db.add_account(service, account_type, label, username, encrypt(password))
        flash(f"Account '{label}' added. Authenticate it before the first scheduled run.", "success")
        return redirect(url_for("main.accounts"))
    return render_template("add_account.html", services=SERVICE_REGISTRY)


@bp.route("/accounts/<int:account_id>/toggle", methods=["POST"])
def toggle_account(account_id):
    account = db.get_account(account_id)
    if account:
        db.set_account_enabled(account_id, not account["enabled"])
    return redirect(url_for("main.accounts"))


@bp.route("/accounts/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    db.delete_account(account_id)
    flash("Account removed.", "success")
    return redirect(url_for("main.accounts"))


@bp.route("/accounts/<int:account_id>/reauth", methods=["POST"])
def reauth_account(account_id):
    account = db.get_account(account_id)
    if not account:
        flash("Account not found.", "error")
        return redirect(url_for("main.accounts"))

    # One auth at a time: all auths share one Playwright CLI session, so a
    # second concurrent attach/detach would tear down the first one's tab.
    with _AUTH_LOCK:
        if any(s["state"] == "pending" for s in _AUTH_STATE.values()):
            flash("An authentication is already in progress. Finish that login first.", "error")
            return redirect(url_for("main.accounts"))
        _AUTH_STATE[account_id] = {"state": "pending"}

    from app.chrome_auth import finish_manual_auth, start_manual_auth

    try:
        start_manual_auth(account)
    except Exception as exc:
        with _AUTH_LOCK:
            _AUTH_STATE.pop(account_id, None)
        log.exception("Could not start browser authentication for account id %s", account_id)
        flash(f"Could not open the authentication tab: {exc}", "error")
        return redirect(url_for("main.accounts"))

    def _finish():
        try:
            finish_manual_auth(account)
            with _AUTH_LOCK:
                _AUTH_STATE.pop(account_id, None)
            log.info("Authentication completed for account id %s.", account_id)
        except Exception as exc:
            db.set_account_needs_reauth(account_id, True)
            with _AUTH_LOCK:
                _AUTH_STATE[account_id] = {"state": "failed", "error": str(exc)[:500]}
            log.exception("Manual authentication failed for account id %s: %s", account_id, exc)

    threading.Thread(target=_finish, daemon=True, name=f"auth-{account_id}").start()
    flash(
        "Authentication tab opened in your existing Chrome. Complete the login there; "
        "this page updates automatically when login is detected.",
        "success",
    )
    return redirect(url_for("main.accounts"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            recipient_emails = [e.strip() for e in request.form.get("recipient_emails", "").split(",") if e.strip()]
            if not recipient_emails or any(not _valid_email(e) for e in recipient_emails):
                raise ValueError("Enter one or more valid recipient email addresses.")
            smtp_port = int(request.form.get("smtp_port") or 587)
            if not 1 <= smtp_port <= 65535:
                raise ValueError("SMTP port must be between 1 and 65535.")
            schedule_type = request.form.get("schedule_type", "monthly")
            schedule_value = request.form.get("schedule_value", "1").strip()
            _validate_schedule(schedule_type, schedule_value)

            fields = dict(
                recipient_emails=recipient_emails,
                smtp_host=request.form.get("smtp_host", "").strip(),
                smtp_port=smtp_port,
                smtp_username=request.form.get("smtp_username", "").strip(),
                smtp_use_tls=1 if request.form.get("smtp_use_tls") == "on" else 0,
                sender_email=request.form.get("sender_email", "").strip(),
                schedule_type=schedule_type,
                schedule_value=schedule_value,
            )
            smtp_password = request.form.get("smtp_password", "")
            if smtp_password:
                fields["encrypted_smtp_password"] = encrypt(smtp_password)
            db.update_settings(**fields)
            scheduler.reschedule()
            flash("Settings saved and scheduler updated.", "success")
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", settings=db.get_settings())


@bp.route("/run-now", methods=["POST"])
def run_now():
    threading.Thread(target=run_all, daemon=True).start()
    flash("Run started in the background. Check Dashboard/Logs for the result.", "success")
    return redirect(url_for("main.dashboard"))


@bp.route("/logs")
def logs():
    return render_template("logs.html", runs=db.list_recent_runs(limit=100), invoices=db.list_processed_invoices())


@bp.route("/logs/invoices/<int:row_id>/delete", methods=["POST"])
def delete_processed(row_id):
    db.delete_processed_invoice(row_id)
    flash("Invoice removed from the processed list; it can be sent again on the next run.", "success")
    return redirect(url_for("main.logs"))


def create_app() -> Flask:
    flask_app = Flask(__name__)
    if WEB_SECRET_KEY_PATH.exists():
        flask_app.secret_key = WEB_SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    else:
        import secrets
        key = secrets.token_urlsafe(32)
        WEB_SECRET_KEY_PATH.write_text(key, encoding="utf-8")
        flask_app.secret_key = key
    flask_app.register_blueprint(bp)
    return flask_app