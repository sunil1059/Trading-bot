"""Structured JSON logging with secret redaction.

Redaction happens in a logging.Filter applied at the handler, so no handler can
emit an unredacted record. It masks:
- values of known secret field names passed via ``extra=``
- secret-looking key=value / "key": "value" patterns inside messages
- exact secret values registered at startup (e.g. the live access token)
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"

SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "access_token",
        "request_token",
        "refresh_token",
        "password",
        "pin",
        "totp",
        "totp_secret",
        "secret",
        "token",
        "authorization",
        "telegram_bot_token",
        "checksum",
        "enctoken",
    }
)

_KEY_ALT = "|".join(sorted((re.escape(k) for k in SECRET_KEYS), key=len, reverse=True))
# key=value, key: value, "key": "value", Authorization: token abc:xyz
_PATTERN = re.compile(
    rf"""(?ix)
    (["']?(?:{_KEY_ALT})["']?\s*[:=]\s*)   # key and separator
    (["']?)                                 # optional opening quote
    ([^"'\s,&}}]+)                          # value
    """
)
# "Authorization: token key:secret" / "Bearer xyz": mask scheme credentials too
_AUTH = re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?)([A-Za-z]+\s+)?([^\s\"',]+)")

_STD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

_registered_lock = threading.Lock()
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret value (e.g. a session token) to mask anywhere in logs."""
    if value and len(value) >= 6:
        with _registered_lock:
            _registered_secrets.add(value)


def redact_text(text: str) -> str:
    text = _AUTH.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}{REDACTED}", text)
    text = _PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    with _registered_lock:
        secrets = list(_registered_secrets)
    for s in secrets:
        text = text.replace(s, REDACTED)
    return text


def redact_value(key: str, value: Any) -> Any:
    if key.lower() in SECRET_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value("", v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        for key, val in list(vars(record).items()):
            if key not in _STD_ATTRS:
                setattr(record, key, redact_value(key, val))
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in vars(record).items():
            if key not in _STD_ATTRS:
                payload[key] = val
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
