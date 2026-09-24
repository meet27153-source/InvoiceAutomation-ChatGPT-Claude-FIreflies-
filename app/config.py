"""Central local configuration and filesystem paths."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
INVOICE_DIR = DATA_DIR / "invoices"
DB_PATH = DATA_DIR / "app.db"
SECRET_KEY_PATH = DATA_DIR / "secret.key"
WEB_SECRET_KEY_PATH = DATA_DIR / "web.secret"
LOG_FILE = LOG_DIR / "app.log"

WEB_HOST = "127.0.0.1"
WEB_PORT = 7000
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30

for directory in (DATA_DIR, LOG_DIR, INVOICE_DIR):
    directory.mkdir(parents=True, exist_ok=True)
