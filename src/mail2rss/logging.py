from __future__ import annotations

import hashlib
import logging as py_logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SECRET_FIELD_NAMES = {"token", "authorization", "body", "body_html", "subject"}


def _redact_secrets(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in SECRET_FIELD_NAMES:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str) -> None:
    py_logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(py_logging, level.upper(), py_logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            _redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(py_logging, level.upper(), py_logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def hash_identifier(value: str, salt: str, length: int = 16) -> str:
    digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
    return digest[:length]
