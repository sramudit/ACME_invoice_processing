"""SQLite persistence: inventory setup + durable run/reconciliation provenance."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .fx import convert_to_usd, fx_rate_asof
from .models import PipelineResult

# Approved-vendor list (names taken from the sample invoices). Fraudster LLC /
# NoProd Industries are intentionally excluded (fraud tests).
_APPROVED_VENDORS = [
    "Precision Parts Ltd.",
    "Global Supply Chain Partners",
    "Atlas Industrial Supply",
    "Widgets Inc.",
    "Consolidated Materials Group",
    "Summit Manufacturing Co.",
    "QuickShip Distributers",
    "Acme Industrial Supplies",
    "TechParts International",
]

# WidgetC / SuperGizmo / MegaSprocket are intentionally absent (unknown-item tests).
_INVENTORY_SEED = [
    ("WidgetA", 15, 250.0, "widget", 5),
    ("WidgetB", 10, 500.0, "widget", 4),
    ("GadgetX", 5, 750.0, "gadget", 3),
    ("FakeItem", 0, 0.0, "unknown", 0),
]

REVIEW_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS review_queue (
    invoice_number TEXT,
    source         TEXT,
    reason         TEXT,
    invoice_path   TEXT,
    requested_at   TEXT,
    status         TEXT DEFAULT 'pending'
)
"""

PIPELINE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    invoice_number    TEXT,
    invoice_path      TEXT,
    vendor            TEXT,
    invoice_date      TEXT,
    currency          TEXT,
    total_native      REAL,
    fx_rate           REAL,
    fx_rate_date      TEXT,
    fx_source         TEXT,
    fx_is_foreign     INTEGER,
    total_usd         REAL,
    validation_passed INTEGER,
    flags             TEXT,   -- JSON array
    decision          TEXT,
    payment_status    TEXT,
    payment_reason    TEXT,
    logs              TEXT,   -- JSON array
    result_json       TEXT,   -- full PipelineResult (audit)
    run_at            TEXT
)
"""

RECONCILIATIONS_DDL = """
CREATE TABLE IF NOT EXISTS reconciliations (
    invoice_number TEXT,
    relationship   TEXT,   -- EXACT_DUPLICATE | SUPERSEDES | CONFLICT
    process_paths  TEXT,   -- JSON array of version(s) routed to the pipeline
    skip_paths     TEXT,   -- JSON array of version(s) dropped as superseded/dup
    reasoning      TEXT,
    run_at         TEXT
)
"""


def setup_inventory(db_path: Path, *, reset: bool = True) -> None:
    """Create and seed the mock inventory database required by the README."""
    if reset and db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                item TEXT PRIMARY KEY,
                stock INTEGER,
                unit_price REAL,
                category TEXT,
                reorder_threshold INTEGER
            )
            """
        )
        cur.executemany("INSERT OR REPLACE INTO inventory VALUES (?, ?, ?, ?, ?)", _INVENTORY_SEED)

        cur.execute("CREATE TABLE IF NOT EXISTS vendors (name TEXT PRIMARY KEY, approved INTEGER)")
        cur.executemany(
            "INSERT OR REPLACE INTO vendors VALUES (?, ?)",
            [(name, 1) for name in _APPROVED_VENDORS],
        )

        # Ledger of already-paid invoices – used to catch duplicate submissions.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paid_invoices (
                invoice_number TEXT PRIMARY KEY,
                vendor TEXT,
                amount REAL,
                paid_at TEXT
            )
            """
        )

        # Append-only receipts ledger: goods received from paid vendor invoices.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_goods (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT,
                vendor TEXT,
                item TEXT,
                quantity INTEGER,
                unit_price REAL,
                received_at TEXT
            )
            """
        )
        cur.execute(REVIEW_QUEUE_DDL)
        conn.commit()
    finally:
        conn.close()


def _result_to_run_row(r: PipelineResult) -> dict:
    """Flatten one PipelineResult into a durable row with dated FX provenance."""
    ex = r.extracted
    currency = ((ex.currency if ex else None) or "USD").upper()
    invoice_date = ex.invoice_date if ex else None
    fx = fx_rate_asof(currency, invoice_date)
    total_native = ex.total if ex and ex.total is not None else None
    total_usd = (
        convert_to_usd(total_native, currency, invoice_date)
        if total_native is not None
        else None
    )
    payment = r.payment_status or {}
    return {
        "invoice_number": ex.invoice_number if ex else None,
        "invoice_path": r.invoice_path,
        "vendor": ex.vendor if ex else None,
        "invoice_date": invoice_date,
        "currency": currency,
        "total_native": total_native,
        "fx_rate": fx.rate,
        "fx_rate_date": fx.as_of,
        "fx_source": fx.source,
        "fx_is_foreign": int(currency != "USD"),
        "total_usd": total_usd,
        "validation_passed": int(bool(r.validation.passed)) if r.validation else None,
        "flags": json.dumps(r.validation.flags if r.validation else []),
        "decision": r.approval.decision.value if r.approval else None,
        "payment_status": payment.get("status"),
        "payment_reason": payment.get("reason"),
        "logs": json.dumps(r.logs),
        "result_json": json.dumps(asdict(r), default=str),
        "run_at": datetime.now().isoformat(timespec="seconds"),
    }


def persist_pipeline_runs(runs: list[PipelineResult], db_path: Path) -> int:
    """Rebuild ``pipeline_runs`` so it mirrors the latest pass (idempotent)."""
    rows = [_result_to_run_row(r) for r in runs]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(PIPELINE_RUNS_DDL)
        conn.execute("DELETE FROM pipeline_runs")
        if rows:
            cols = list(rows[0].keys())
            placeholders = ", ".join(["?"] * len(cols))
            conn.executemany(
                f"INSERT INTO pipeline_runs ({', '.join(cols)}) VALUES ({placeholders})",
                [tuple(row[c] for c in cols) for row in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _reconciliation_invoice_number(r: PipelineResult) -> Optional[str]:
    """A real result carries the number on ``extracted``; a CONFLICT stub encodes
    it in its synthetic path ``(reconciliation:INV-XXXX)``."""
    if r.extracted:
        return r.extracted.invoice_number
    prefix = "(reconciliation:"
    if r.invoice_path.startswith(prefix):
        return r.invoice_path[len(prefix):].rstrip(")")
    return None


def result_to_document(r: PipelineResult) -> dict:
    """A self-contained JSON-ready record: the full result plus flat FX provenance."""
    row = _result_to_run_row(r)
    return {
        "invoice_number": row["invoice_number"],
        "invoice_path": row["invoice_path"],
        "decision": row["decision"],
        "payment_status": row["payment_status"],
        "payment_reason": row["payment_reason"],
        "validation_passed": row["validation_passed"],
        "flags": json.loads(row["flags"]),
        "currency": row["currency"],
        "total_native": row["total_native"],
        "total_usd": row["total_usd"],
        "fx": {
            "rate": row["fx_rate"],
            "as_of": row["fx_rate_date"],
            "source": row["fx_source"],
            "is_foreign": bool(row["fx_is_foreign"]),
        },
        "logs": r.logs,
        "reconciliation": r.reconciliation,
        "result": asdict(r),
    }


def persist_reconciliations(runs: list[PipelineResult], db_path: Path) -> int:
    """Rebuild ``reconciliations`` from results carrying a verdict (one row/number)."""
    rows_by_number: dict[str, dict] = {}
    for r in runs:
        if not r.reconciliation:
            continue
        number = _reconciliation_invoice_number(r)
        if number is None or number in rows_by_number:
            continue  # first occurrence wins; verdict is identical across versions
        recon = r.reconciliation
        rows_by_number[number] = {
            "invoice_number": number,
            "relationship": recon.get("relationship"),
            "process_paths": json.dumps(recon.get("process_paths", [])),
            "skip_paths": json.dumps(recon.get("skip_paths", [])),
            "reasoning": recon.get("reasoning"),
            "run_at": datetime.now().isoformat(timespec="seconds"),
        }
    rows = list(rows_by_number.values())
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(RECONCILIATIONS_DDL)
        conn.execute("DELETE FROM reconciliations")
        if rows:
            cols = list(rows[0].keys())
            placeholders = ", ".join(["?"] * len(cols))
            conn.executemany(
                f"INSERT INTO reconciliations ({', '.join(cols)}) VALUES ({placeholders})",
                [tuple(row[c] for c in cols) for row in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)
