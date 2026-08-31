"""Manual review queue: a shared escape hatch any agent can pull."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .persistence import REVIEW_QUEUE_DDL
from .models import ReviewRequest

logger = logging.getLogger(__name__)


def request_manual_review(
    invoice_number: str,
    source: str,
    reason: str,
    db_path: Path,
    invoice_path: Optional[str] = None,
) -> ReviewRequest:
    """Escalate an invoice to a human reviewer. Callable by any agent or node.

    The approval agent calls this when it returns NEEDS_REVIEW; the duplicate
    reconciliation agent calls it on a CONFLICT. Persisted to a durable SQLite
    worklist so a flagged invoice is never silently paid or dropped.
    """
    req = ReviewRequest(invoice_number, source, reason, invoice_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(REVIEW_QUEUE_DDL)
        conn.execute(
            "INSERT INTO review_queue (invoice_number, source, reason, invoice_path, requested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (req.invoice_number, req.source, req.reason, req.invoice_path, req.requested_at),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "Manual review requested for %s (raised by %s): %s",
        invoice_number,
        source,
        reason,
        extra={"stage": "manual_review", "invoice_number": invoice_number},
    )
    return req
