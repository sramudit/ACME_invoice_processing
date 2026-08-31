"""Shared data models for the invoice-processing pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Decision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


@dataclass
class LineItem:
    item: str
    quantity: int
    unit_price: Optional[float] = None
    note: Optional[str] = None  # parenthetical context, e.g. "rush order"


@dataclass
class ExtractedInvoice:
    invoice_number: str
    vendor: str
    due_date: Optional[str]
    line_items: list[LineItem]
    invoice_date: Optional[str] = None
    subtotal: Optional[float] = None
    tax_rate: Optional[float] = 0.0
    tax_amount: Optional[float] = 0.0
    shipping_amount: Optional[float] = 0.0
    total: Optional[float] = None
    currency: str = "USD"
    raw_notes: str = ""
    anomalies: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    passed: bool
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalResult:
    decision: Decision
    reason: str
    scrutiny_level: str = "standard"
    critique_history: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FxRate:
    """A single dated FX quote used to convert a currency to USD."""

    currency: str
    rate: float      # native amount × rate → USD
    as_of: str       # effective date of the quote actually used (YYYY-MM-DD)
    source: str      # provenance string for the audit trail


@dataclass
class ContextReviewResult:
    verdict: str        # JUSTIFIED | UNJUSTIFIED | INSUFFICIENT_CONTEXT
    reasoning: str


@dataclass
class ReconciliationResult:
    relationship: str          # EXACT_DUPLICATE | SUPERSEDES | CONFLICT
    process_paths: list[str]   # version(s) routed into the payment pipeline
    skip_paths: list[str]      # version(s) dropped as duplicate/superseded
    reasoning: str


@dataclass
class ReviewRequest:
    invoice_number: str
    source: str        # agent that raised it, e.g. "approval" | "reconciliation"
    reason: str
    invoice_path: Optional[str] = None
    requested_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )


@dataclass
class PipelineResult:
    invoice_path: str
    extracted: Optional[ExtractedInvoice] = None
    validation: Optional[ValidationResult] = None
    approval: Optional[ApprovalResult] = None
    payment_status: Optional[dict] = None
    # When an invoice number arrives more than once, the duplicate-reconciliation
    # agent's verdict is attached here so the reasoning travels WITH the result
    # into asdict(result) / the JSON logs / the audit table.
    reconciliation: Optional[dict] = None
    logs: list[str] = field(default_factory=list)
