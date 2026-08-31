"""Validation flags the README's scenario matrix."""
from __future__ import annotations

from acme_invoices.ingest import ingest_invoice
from acme_invoices.validate import validate_invoice

from conftest import invoice


def _flags(name: str, db_path) -> list[str]:
    inv = ingest_invoice(invoice(name), db_path)
    return validate_invoice(inv, db_path).flags


def test_clean_invoice_passes(db_path):
    inv = ingest_invoice(invoice("invoice_1001.txt"), db_path)
    result = validate_invoice(inv, db_path)
    assert result.passed, result.flags


def test_stock_mismatch(db_path):
    flags = _flags("invoice_1002.txt", db_path)  # 20x GadgetX, only 5 in stock
    assert any(f.startswith("STOCK_MISMATCH") for f in flags)


def test_zero_stock_item(db_path):
    flags = _flags("invoice_1003.txt", db_path)  # FakeItem, 0 stock
    assert any(f.startswith("OUT_OF_STOCK") for f in flags)


def test_unknown_item_txt(db_path):
    flags = _flags("invoice_1008.txt", db_path)  # SuperGizmo / MegaSprocket
    assert any(f.startswith("UNKNOWN_ITEM") for f in flags)


def test_unknown_item_json(db_path):
    flags = _flags("invoice_1016.json", db_path)  # WidgetC
    assert any(f.startswith("UNKNOWN_ITEM") for f in flags)


def test_negative_quantity(db_path):
    flags = _flags("invoice_1009.json", db_path)
    assert any(f.startswith("NEGATIVE_QTY") for f in flags)


def test_duplicate_invoice_flag(db_path):
    import sqlite3

    inv = ingest_invoice(invoice("invoice_1001.txt"), db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO paid_invoices VALUES (?, ?, ?, ?)",
        (inv.invoice_number, inv.vendor, 5000.0, "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    flags = validate_invoice(inv, db_path).flags
    assert any(f.startswith("DUPLICATE_INVOICE") for f in flags)
