"""Approval rules, the Grok reflection loop (mocked), and price-variance review."""
from __future__ import annotations

from acme_invoices.approve import (
    approve_invoice,
    approve_invoice_base,
    grok_critique,
    request_context_review,
)
from acme_invoices.ingest import ingest_invoice
from acme_invoices.models import Decision, ExtractedInvoice, LineItem, ValidationResult
from acme_invoices.validate import validate_invoice

from conftest import FakeLLM, invoice


def _clean_high_value() -> ExtractedInvoice:
    """All items within stock, prices match catalog, vendor approved, total > $10k."""
    return ExtractedInvoice(
        invoice_number="INV-HV",
        vendor="Widgets Inc.",
        due_date="2026-02-01",
        invoice_date="2026-01-15",
        line_items=[
            LineItem("WidgetA", 15, 250.0),
            LineItem("WidgetB", 10, 500.0),
            LineItem("GadgetX", 5, 750.0),
        ],
        total=12500.0,
        currency="USD",
    )


def test_rejects_on_validation_failure(db_path):
    inv = ingest_invoice(invoice("invoice_1002.txt"), db_path)  # stock mismatch + unknown vendor
    validation = validate_invoice(inv, db_path)
    result = approve_invoice_base(inv, validation, llm=None)
    assert result.decision == Decision.REJECTED


def test_high_value_needs_review_offline(db_path):
    inv = _clean_high_value()
    validation = validate_invoice(inv, db_path)
    assert validation.passed
    result = approve_invoice_base(inv, validation, llm=None)
    assert result.decision == Decision.NEEDS_REVIEW


def test_grok_critique_maps_verb(db_path):
    inv = ingest_invoice(invoice("invoice_1001.txt"), db_path)
    validation = validate_invoice(inv, db_path)
    llm = FakeLLM('{"decision": "REJECT", "reasoning": "looks fraudulent"}')
    critique = grok_critique(inv, validation, llm)
    assert critique["decision"] == Decision.REJECTED
    assert "fraudulent" in critique["reasoning"]


def test_grok_can_escalate_clean_invoice(db_path):
    inv = ingest_invoice(invoice("invoice_1001.txt"), db_path)
    validation = validate_invoice(inv, db_path)
    llm = FakeLLM('{"decision": "NEEDS_REVIEW", "reasoning": "second look needed"}')
    result = approve_invoice_base(inv, validation, llm)
    assert result.decision == Decision.NEEDS_REVIEW


def test_price_variance_justified_offline(db_path):
    inv = ingest_invoice(invoice("invoice_1010.txt"), db_path)  # rush-order premium
    validation = validate_invoice(inv, db_path)
    assert not validation.passed
    result = approve_invoice(inv, validation, invoice_path=invoice("invoice_1010.txt"), llm=None)
    assert result.decision == Decision.APPROVED  # "rush" keyword justifies the premium


def test_context_review_unjustified_escalates(db_path):
    inv = ingest_invoice(invoice("invoice_1010.txt"), db_path)
    validation = validate_invoice(inv, db_path)
    llm = FakeLLM('{"verdict": "UNJUSTIFIED", "reasoning": "no documented terms"}')
    review = request_context_review(inv, validation.flags, invoice("invoice_1010.txt"), llm)
    assert review.verdict == "UNJUSTIFIED"
    result = approve_invoice(inv, validation, invoice_path=invoice("invoice_1010.txt"), llm=llm)
    assert result.decision == Decision.NEEDS_REVIEW


def test_empty_validation_passes_through_approved(db_path):
    inv = ingest_invoice(invoice("invoice_1001.txt"), db_path)
    validation = ValidationResult(passed=True)
    result = approve_invoice_base(inv, validation, llm=None)
    assert result.decision == Decision.APPROVED
