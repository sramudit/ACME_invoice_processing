"""End-to-end pipeline + CLI, all offline (deterministic, no network)."""
from __future__ import annotations

import json
import sqlite3

from acme_invoices.cli import main
from acme_invoices.config import Settings
from acme_invoices.models import Decision
from acme_invoices.persistence import result_to_document
from acme_invoices.pipeline import persist_results, process_folder, process_single

from conftest import DATA_DIR, invoice


def test_process_single_pays_clean_invoice(settings):
    result = process_single(invoice("invoice_1001.txt"), settings, llm=None)
    assert result.approval.decision == Decision.APPROVED
    assert result.payment_status["status"] == "success"


def test_process_single_rejects_bad_invoice(settings):
    result = process_single(invoice("invoice_1002.txt"), settings, llm=None)
    assert result.payment_status["status"] == "rejected"


def test_process_folder_and_persist(settings):
    results = process_folder(settings, llm=None)
    assert results
    runs, recons = persist_results(results, settings)
    assert runs == len(results)

    conn = sqlite3.connect(settings.db_path)
    n = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    conn.close()
    assert n == len(results)
    # At least one invoice should be paid end-to-end.
    assert any((r.payment_status or {}).get("status") == "success" for r in results)


def test_result_document_is_json_serializable(settings):
    result = process_single(invoice("invoice_1014.xml"), settings, llm=None)
    doc = result_to_document(result)
    text = json.dumps(doc, default=str)
    assert "fx" in doc
    assert doc["currency"] == "EUR"
    assert doc["fx"]["is_foreign"] is True
    assert json.loads(text)["invoice_number"] == "INV-1014"


def test_cli_single_invoice(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "")  # ensure offline
    db = tmp_path / "inventory.db"
    out = tmp_path / "results.json"
    code = main([
        f"--invoice_path={invoice('invoice_1001.txt')}",
        f"--db={db}",
        "--reset-db",
        "--offline",
        "--log-format=json",
        f"--out={out}",
    ])
    assert code == 0
    docs = json.loads(out.read_text())
    assert docs[0]["invoice_number"] == "INV-1001"
    assert docs[0]["payment_status"] == "success"


def test_cli_offline_does_not_launch_dashboard(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("acme_invoices.cli._launch_dashboard", lambda s: calls.append(s))
    db = tmp_path / "inventory.db"
    code = main([
        f"--invoice_path={invoice('invoice_1001.txt')}",
        f"--db={db}",
        "--reset-db",
        "--offline",
    ])
    assert code == 0
    assert calls == []  # no Grok key + offline => no auto-launch


def test_cli_force_dashboard_launches(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("acme_invoices.cli._launch_dashboard", lambda s: calls.append(s))
    db = tmp_path / "inventory.db"
    code = main([
        f"--invoice_path={invoice('invoice_1001.txt')}",
        f"--db={db}",
        "--reset-db",
        "--offline",
        "--dashboard",
    ])
    assert code == 0
    assert len(calls) == 1  # explicit --dashboard forces the launch

