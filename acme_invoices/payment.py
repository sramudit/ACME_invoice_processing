"""Payment: mock payment + paid/received-goods ledgers."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import ApprovalResult, Decision, ExtractedInvoice

logger = logging.getLogger(__name__)


def record_paid_invoice(extracted: ExtractedInvoice, amount: float, db_path: Path) -> None:
    """Persist a paid invoice (duplicate ledger) and record the goods received.

    The ``paid_invoices`` insert and the ``received_goods`` rows commit in one
    transaction. Receipts are only written on the first payment (INSERT OR IGNORE
    rowcount == 1), so a duplicate submission can never double-count goods.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO paid_invoices VALUES (?, ?, ?, ?)",
            (
                extracted.invoice_number,
                extracted.vendor,
                amount,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        if cur.rowcount == 1:
            received_at = datetime.now().isoformat(timespec="seconds")
            conn.executemany(
                """
                INSERT INTO received_goods
                    (invoice_number, vendor, item, quantity, unit_price, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        extracted.invoice_number,
                        extracted.vendor,
                        li.item,
                        li.quantity,
                        li.unit_price,
                        received_at,
                    )
                    for li in extracted.line_items
                ],
            )
        conn.commit()
    finally:
        conn.close()


def mock_payment(vendor: str, amount: float) -> dict:
    """Mock payment API from the README."""
    logger.info("Paid %s to %s", amount, vendor, extra={"stage": "pay"})
    return {"status": "success", "vendor": vendor, "amount": amount}


def execute_payment_or_reject(
    extracted: ExtractedInvoice, approval: ApprovalResult, db_path: Path
) -> dict:
    if approval.decision == Decision.APPROVED:
        amount = extracted.total or 0.0
        payment = mock_payment(extracted.vendor, amount)
        record_paid_invoice(extracted, amount, db_path)
        return payment
    return {
        "status": "rejected",
        "reason": approval.reason,
        "decision": approval.decision.value,
    }
