"""Duplicate reconciliation: offline fallback + CONFLICT escalation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from acme_invoices.ingest import ingest_invoice
from acme_invoices.reconcile import reconcile_duplicates

from conftest import FakeLLM, invoice


def _versions(db_path, *names):
    return [(invoice(n), ingest_invoice(invoice(n), db_path)) for n in names]


def test_offline_fallback_keeps_most_complete(db_path):
    versions = _versions(db_path, "invoice_1004.json", "invoice_1004_revised.json")
    result = reconcile_duplicates("INV-1004", versions, db_path, llm=None)
    assert result.relationship == "SUPERSEDES"
    assert len(result.process_paths) == 1
    assert len(result.skip_paths) == 1


def test_llm_supersedes_selects_authoritative(db_path):
    versions = _versions(db_path, "invoice_1004.json", "invoice_1004_revised.json")
    reply = (
        '{"relationship": "SUPERSEDES", '
        '"authoritative_file": "invoice_1004_revised.json", '
        '"reasoning": "R1 revision adds a line item"}'
    )
    result = reconcile_duplicates("INV-1004", versions, db_path, llm=FakeLLM(reply))
    assert result.relationship == "SUPERSEDES"
    assert result.process_paths == [str(invoice("invoice_1004_revised.json"))]


def test_conflict_escalates_to_review_queue(db_path):
    versions = _versions(db_path, "invoice_1004.json", "invoice_1004_revised.json")
    reply = '{"relationship": "CONFLICT", "authoritative_file": null, "reasoning": "totals disagree"}'
    result = reconcile_duplicates("INV-1004", versions, db_path, llm=FakeLLM(reply))
    assert result.relationship == "CONFLICT"
    assert result.process_paths == []

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT source FROM review_queue WHERE invoice_number = 'INV-1004'"
    ).fetchall()
    conn.close()
    assert any(r[0] == "reconciliation" for r in rows)
