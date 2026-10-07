"""Logging configuration with secret redaction applied at the handler level."""

from __future__ import annotations

import logging

from .http_client import redact


class RedactingFilter(logging.Filter):
    """Scrubs the API key from messages and exception text of every record."""

    def __init__(self, secret: str | None = None) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secret)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secret)
        return True


def configure_logging(level: str, secret: str | None) -> None:
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    redactor = RedactingFilter(secret)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redactor)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
