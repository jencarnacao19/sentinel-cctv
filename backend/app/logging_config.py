"""Structured logging with mandatory secret redaction.

Every Sentinel logger lives under a category (``sentinel.camera``,
``sentinel.recording``, ...) so the diagnostics page can filter by subsystem.
A redaction filter is attached at the root: even a third-party library that logs
an RTSP URL with credentials cannot leak them to disk.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

from app.security.redaction import redact_mapping, redact_text

Category = Literal[
    "SYSTEM",
    "CAMERA",
    "RECORDING",
    "MOTION",
    "STORAGE",
    "API",
    "HOME_ASSISTANT",
    "GO2RTC",
    "DISCOVERY",
]

CATEGORIES: tuple[Category, ...] = (
    "SYSTEM",
    "CAMERA",
    "RECORDING",
    "MOTION",
    "STORAGE",
    "API",
    "HOME_ASSISTANT",
    "GO2RTC",
    "DISCOVERY",
)

_LOGGER_NAMES: dict[Category, str] = {
    "SYSTEM": "sentinel.system",
    "CAMERA": "sentinel.camera",
    "RECORDING": "sentinel.recording",
    "MOTION": "sentinel.motion",
    "STORAGE": "sentinel.storage",
    "API": "sentinel.api",
    "HOME_ASSISTANT": "sentinel.home_assistant",
    "GO2RTC": "sentinel.go2rtc",
    "DISCOVERY": "sentinel.discovery",
}

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class ContextLogger(logging.LoggerAdapter[logging.Logger]):
    """A logger that cannot be crashed by an unlucky ``extra`` key.

    ``logging`` raises ``KeyError`` when an ``extra`` entry would shadow a
    built-in ``LogRecord`` attribute -- ``process``, ``module``, ``name`` and a
    dozen others. That turns a harmless log line into an exception, and in a
    supervisor loop it means the thing being supervised never starts. Colliding
    keys are renamed rather than dropped, so the context survives and nothing
    raises.
    """

    def process(
        self, msg: object, kwargs: MutableMapping[str, Any]
    ) -> tuple[object, MutableMapping[str, Any]]:
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"ctx_{key}" if key in _RESERVED else key): value for key, value in extra.items()
            }
        return msg, kwargs


def get_logger(category: Category) -> ContextLogger:
    """Return the canonical logger for a subsystem."""
    return ContextLogger(logging.getLogger(_LOGGER_NAMES[category]), {})


def category_for_logger(name: str) -> str:
    for category, logger_name in _LOGGER_NAMES.items():
        if name == logger_name or name.startswith(logger_name + "."):
            return category
    return "SYSTEM" if name.startswith("sentinel") else "EXTERNAL"


class RedactionFilter(logging.Filter):
    """Strip credentials from the message and from every extra field."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_text(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_text(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, str):
                record.__dict__[key] = redact_text(value)
        return True


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line output for container logs."""

    default_time_format = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record)} "
            f"{record.levelname:<8} "
            f"{category_for_logger(record.name):<14} "
            f"{record.getMessage()}"
        )
        extras = self._extras(record)
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return redact_text(base)

    @staticmethod
    def _extras(record: logging.LogRecord) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }


class JsonFormatter(logging.Formatter):
    """Machine-readable output, used when logs are shipped elsewhere."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "category": category_for_logger(record.name),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(ConsoleFormatter._extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Redaction happens per value, before serialisation. Running it over the
        # finished JSON string would let a pattern such as `token=<value>"`
        # swallow the closing quote and emit a document no parser can read.
        return json.dumps(redact_mapping(payload), default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install Sentinel's handlers on the root logger. Idempotent."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    handler.addFilter(RedactionFilter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Access logs duplicate what the API logger already records, and uvicorn's
    # error logger would otherwise double-print through its own handler.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("uvicorn", "uvicorn.error", "httpx", "httpcore", "zeroconf"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
