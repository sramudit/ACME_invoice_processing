"""Structured observability: JSON-lines events for CI, Rich console for humans."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Optional

LOGGER_NAME = "acme_invoices"


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per log record: {ts, level, stage, event, ...}."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "stage": getattr(record, "stage", record.name.rsplit(".", 1)[-1]),
            "event": record.getMessage(),
        }
        for key in ("invoice_number", "detail"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(
    log_format: str = "rich",
    log_file: Optional[str] = None,
    verbose: bool = False,
) -> logging.Logger:
    """Configure the package logger. ``log_format`` is 'json' or 'rich'."""
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.propagate = False

    if log_format == "json":
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLinesFormatter())
    else:
        from rich.logging import RichHandler

        handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
    root.addHandler(handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonLinesFormatter())
        root.addHandler(file_handler)

    return root


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    stage: Optional[str] = None,
    invoice_number: Optional[str] = None,
    detail: object = None,
    level: int = logging.INFO,
) -> None:
    """Emit a structured event with optional stage / invoice / detail fields."""
    extra: dict[str, object] = {}
    if stage is not None:
        extra["stage"] = stage
    if invoice_number is not None:
        extra["invoice_number"] = invoice_number
    if detail is not None:
        extra["detail"] = detail
    logger.log(level, event, extra=extra)
