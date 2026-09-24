"""SQLite persistence layer for the local invoice automation application."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import DB_PATH


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'default',
                label TEXT NOT NULL,
                username TEXT NOT NULL,
                encrypted_password TEXT NOT NULL DEFAULT '',
                encrypted_session TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                needs_reauth INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                recipient_emails TEXT NOT NULL DEFAULT '[]',
                smtp_host TEXT,
                smtp_port INTEGER,
                smtp_username TEXT,
                encrypted_smtp_password TEXT,
                smtp_use_tls INTEGER NOT NULL DEFAULT 1,
                sender_email TEXT,
                schedule_type TEXT NOT NULL DEFAULT 'monthly',
                schedule_value TEXT NOT NULL DEFAULT '1'
            );

            CREATE TABLE IF NOT EXISTS processed_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                invoice_external_id TEXT NOT NULL,
                invoice_date TEXT,
                emailed_at TEXT NOT NULL,
                UNIQUE(account_id, invoice_external_id)
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                account_label TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                detail TEXT
            );
            """
        )

        # Lightweight migration for databases created by older versions.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "account_type" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT 'default'")
        if "needs_reauth" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN needs_reauth INTEGER NOT NULL DEFAULT 1")
        # Old versions treated an account as ready even when no browser session existed.
        # Require authentication for such accounts after upgrade.
        conn.execute("UPDATE accounts SET needs_reauth = 1 WHERE encrypted_session IS NULL")

        conn.execute("INSERT OR IGNORE INTO settings (id, recipient_emails) VALUES (1, '[]')")


def add_account(service: str, account_type: str, label: str, username: str, encrypted_password: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO accounts
               (service, account_type, label, username, encrypted_password,
                enabled, needs_reauth, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)""",
            (service, account_type, label, username, encrypted_password, now_iso(), now_iso()),
        )
        return cur.lastrowid


def update_account_session(account_id: int, encrypted_session: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET encrypted_session = ?, needs_reauth = 0, updated_at = ? WHERE id = ?",
            (encrypted_session, now_iso(), account_id),
        )


def clear_account_session(account_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET encrypted_session = NULL, needs_reauth = 1, updated_at = ? WHERE id = ?",
            (now_iso(), account_id),
        )


def set_account_needs_reauth(account_id: int, needs_reauth: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET needs_reauth = ?, updated_at = ? WHERE id = ?",
            (1 if needs_reauth else 0, now_iso(), account_id),
        )


def set_account_enabled(account_id: int, enabled: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now_iso(), account_id),
        )


def delete_account(account_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


def list_accounts(enabled_only: bool = False):
    with get_conn() as conn:
        query = "SELECT * FROM accounts"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        return [dict(row) for row in conn.execute(query).fetchall()]


def get_account(account_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None


def get_settings() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        settings = dict(row)
        settings["recipient_emails"] = json.loads(settings["recipient_emails"] or "[]")
        return settings


def update_settings(**fields):
    if "recipient_emails" in fields and isinstance(fields["recipient_emails"], list):
        fields["recipient_emails"] = json.dumps(fields["recipient_emails"])
    if not fields:
        return
    allowed = {
        "recipient_emails", "smtp_host", "smtp_port", "smtp_username",
        "encrypted_smtp_password", "smtp_use_tls", "sender_email",
        "schedule_type", "schedule_value",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown settings fields: {sorted(unknown)}")
    columns = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE settings SET {columns} WHERE id = 1", list(fields.values()))


def is_invoice_processed(account_id: int, invoice_external_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_invoices WHERE account_id = ? AND invoice_external_id = ?",
            (account_id, invoice_external_id),
        ).fetchone()
        return row is not None


def mark_invoice_processed(account_id: int, invoice_external_id: str, invoice_date: str = None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO processed_invoices
               (account_id, invoice_external_id, invoice_date, emailed_at)
               VALUES (?, ?, ?, ?)""",
            (account_id, invoice_external_id, invoice_date, now_iso()),
        )
        return cur.rowcount == 1


def list_processed_invoices(limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pi.*, a.label AS account_label
               FROM processed_invoices pi
               LEFT JOIN accounts a ON a.id = pi.account_id
               ORDER BY pi.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_processed_invoice(row_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM processed_invoices WHERE id = ?", (row_id,))


def start_run(account_id: int, account_label: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO run_log (account_id, account_label, started_at, status)
               VALUES (?, ?, ?, 'running')""",
            (account_id, account_label, now_iso()),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE run_log SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (now_iso(), status, detail[:2000], run_id),
        )


def list_recent_runs(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
