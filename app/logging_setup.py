"""Central logging with credential redaction."""
import logging
import re
from app.config import LOG_FILE

_REDACT_PATTERNS = [
    re.compile(r'(password["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+', re.I),
    re.compile(r'(token["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+', re.I),
    re.compile(r'(cookie["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+', re.I),
    re.compile(r'(authorization["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+', re.I),
]


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for pattern in _REDACT_PATTERNS:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging():
    logger = logging.getLogger("invoice_automation")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    for handler in (file_handler, console_handler):
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    return logger


log = setup_logging()
