"""Structured logging: JSON formatter, file handler, and log_event fields."""
from __future__ import annotations

import json
import logging

from acme_invoices.logging_config import (
    LOGGER_NAME,
    JsonLinesFormatter,
    configure_logging,
    log_event,
)


def _record(**extra):
    rec = logging.LogRecord(
        name=LOGGER_NAME, level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_includes_optional_fields():
    line = JsonLinesFormatter().format(
        _record(stage="pay", invoice_number="INV-1", detail={"x": 1})
    )
    doc = json.loads(line)
    assert doc["event"] == "hello"
    assert doc["stage"] == "pay"
    assert doc["invoice_number"] == "INV-1"
    assert doc["detail"] == {"x": 1}


def test_json_formatter_defaults_stage_to_logger():
    doc = json.loads(JsonLinesFormatter().format(_record()))
    assert doc["stage"] == LOGGER_NAME
    assert "invoice_number" not in doc


def test_configure_logging_json_and_file(tmp_path):
    log_file = tmp_path / "run.jsonl"
    logger = configure_logging(log_format="json", log_file=str(log_file), verbose=True)
    assert logger.level == logging.DEBUG
    log_event(logger, "did a thing", stage="cli", invoice_number="INV-9", detail="ok")
    for h in logger.handlers:
        h.flush()
    doc = json.loads(log_file.read_text().strip().splitlines()[-1])
    assert doc["event"] == "did a thing"
    assert doc["invoice_number"] == "INV-9"


def test_configure_logging_rich_does_not_crash():
    logger = configure_logging(log_format="rich")
    log_event(logger, "rich path")
    assert logger.handlers
