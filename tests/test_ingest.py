"""Ingestion across all formats + item-name canonicalization."""
from __future__ import annotations

from acme_invoices.ingest import (
    canonicalize_item,
    catalog_item_index,
    clean_number,
    detect_currency,
    ingest_invoice,
    invoice_files,
    normalize_date,
)

from conftest import DATA_DIR, invoice


def test_clean_number_handles_symbols_and_ocr():
    assert clean_number("$1,250.00") == 1250.0
    assert clean_number("2O0") == 200.0  # OCR letter O -> zero
    assert clean_number("") is None
    assert clean_number(None) is None


def test_normalize_date_variants():
    assert normalize_date("2026-01-15") == "2026-01-15"
    assert normalize_date("Jan 30 2026") == "2026-01-30"
    assert normalize_date("yesterday") == "yesterday"  # unparseable retained


def test_detect_currency():
    assert detect_currency("Total: €475") == "EUR"
    assert detect_currency("Total: $500") == "USD"
    assert detect_currency("") == "USD"


def test_json_ingest(db_path):
    inv = ingest_invoice(invoice("invoice_1001.txt"), db_path)  # txt via MockLLM
    assert inv.invoice_number == "INV-1001"
    assert inv.vendor == "Widgets Inc."
    assert any(li.item == "WidgetA" for li in inv.line_items)


def test_all_sample_formats_parse(db_path):
    files = invoice_files(DATA_DIR)
    assert len(files) >= 15
    parsed = [ingest_invoice(p, db_path) for p in files]
    # Every parse yields an invoice number and at least attempts line items.
    assert all(p.invoice_number for p in parsed)


def test_canonicalization_maps_spaced_names(db_path):
    index = catalog_item_index(db_path)
    assert canonicalize_item("Widget A", index) == "WidgetA"
    assert canonicalize_item("gadget x", index) == "GadgetX"
    assert canonicalize_item("SuperGizmo", index) == "SuperGizmo"  # unknown untouched


def test_xml_currency_is_foreign(db_path):
    inv = ingest_invoice(invoice("invoice_1014.xml"), db_path)
    assert inv.currency == "EUR"
