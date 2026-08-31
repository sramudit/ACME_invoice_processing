"""LangGraph orchestration: ingest → validate → approve → (context_review) →
pay / manual_review / reject."""
from __future__ import annotations

import logging
import operator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .approve import approve_invoice_base, only_price_variance, resolve_price_variance
from .config import Settings
from .ingest import ingest_invoice
from .llm import LLMClient
from .models import (
    ApprovalResult,
    Decision,
    ExtractedInvoice,
    PipelineResult,
    ValidationResult,
)
from .payment import execute_payment_or_reject
from .review import request_manual_review
from .validate import validate_invoice

logger = logging.getLogger(__name__)


class PipelineState(TypedDict):
    """Shared state threaded through the graph. ``logs`` accumulates across nodes."""

    invoice_path: str
    extracted: Optional[ExtractedInvoice]
    validation: Optional[ValidationResult]
    approval: Optional[ApprovalResult]
    payment_status: Optional[dict]
    logs: Annotated[list[str], operator.add]


def _ts(msg: str) -> str:
    return f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"


def build_invoice_graph(settings: Settings, llm: Optional[LLMClient] = None):
    """Compile the pipeline graph with nodes bound to this run's settings + LLM."""
    db_path = settings.db_path

    def ingest_node(state: PipelineState) -> dict:
        path = Path(state["invoice_path"])
        try:
            extracted = ingest_invoice(path, db_path)
            logs = [
                _ts(
                    f"Ingested {extracted.invoice_number} | vendor={extracted.vendor} | "
                    f"items={len(extracted.line_items)} | total={extracted.total}"
                )
            ]
            if extracted.anomalies:
                logs.append(_ts(f"Anomalies: {extracted.anomalies}"))
            return {"extracted": extracted, "logs": logs}
        except Exception as e:
            return {"extracted": None, "logs": [_ts(f"INGESTION FAILED: {e}")]}

    def validate_node(state: PipelineState) -> dict:
        validation = validate_invoice(state["extracted"], db_path)
        return {
            "validation": validation,
            "logs": [_ts(f"Validation passed={validation.passed} | flags={validation.flags}")],
        }

    def approve_node(state: PipelineState) -> dict:
        approval = approve_invoice_base(state["extracted"], state["validation"], llm)
        return {"approval": approval, "logs": [_ts(f"Approval: {approval.decision.value} | {approval.reason}")]}

    def context_review_node(state: PipelineState) -> dict:
        approval = resolve_price_variance(
            state["extracted"], state["validation"], state["invoice_path"], llm
        )
        return {
            "approval": approval,
            "logs": [_ts(f"Context review: {approval.decision.value} | {approval.reason}")],
        }

    def pay_node(state: PipelineState) -> dict:
        payment = execute_payment_or_reject(state["extracted"], state["approval"], db_path)
        return {"payment_status": payment, "logs": [_ts(f"Final status: {payment}")]}

    def manual_review_node(state: PipelineState) -> dict:
        approval = state["approval"]
        extracted = state["extracted"]
        request_manual_review(
            invoice_number=extracted.invoice_number,
            source="approval",
            reason=approval.reason,
            db_path=db_path,
            invoice_path=state["invoice_path"],
        )
        payment = {"status": "held", "reason": approval.reason, "decision": approval.decision.value}
        return {"payment_status": payment, "logs": [_ts(f"Manual review requested: {approval.reason}")]}

    def reject_node(state: PipelineState) -> dict:
        approval = state["approval"]
        payment = {"status": "rejected", "reason": approval.reason, "decision": approval.decision.value}
        return {"payment_status": payment, "logs": [_ts(f"Rejected ({approval.decision.value}): {approval.reason}")]}

    def after_ingest(state: PipelineState) -> str:
        return "validate" if state.get("extracted") is not None else END

    def after_approval(state: PipelineState) -> str:
        validation = state["validation"]
        decision = state["approval"].decision
        if not validation.passed and only_price_variance(validation.flags):
            return "context_review"
        if decision == Decision.APPROVED:
            return "pay"
        if decision == Decision.NEEDS_REVIEW:
            return "manual_review"
        return "reject"

    def after_context_review(state: PipelineState) -> str:
        return "pay" if state["approval"].decision == Decision.APPROVED else "manual_review"

    builder = StateGraph(PipelineState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("validate", validate_node)
    builder.add_node("approve", approve_node)
    builder.add_node("context_review", context_review_node)
    builder.add_node("pay", pay_node)
    builder.add_node("manual_review", manual_review_node)
    builder.add_node("reject", reject_node)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges("ingest", after_ingest, {"validate": "validate", END: END})
    builder.add_edge("validate", "approve")
    builder.add_conditional_edges(
        "approve",
        after_approval,
        {"pay": "pay", "context_review": "context_review", "manual_review": "manual_review", "reject": "reject"},
    )
    builder.add_conditional_edges(
        "context_review", after_context_review, {"pay": "pay", "manual_review": "manual_review"}
    )
    builder.add_edge("pay", END)
    builder.add_edge("manual_review", END)
    builder.add_edge("reject", END)

    return builder.compile()


def run_pipeline(invoice_path: str | Path, graph) -> PipelineResult:
    """Invoke the compiled graph for one invoice; adapt state -> PipelineResult."""
    path = Path(invoice_path)
    final = graph.invoke(
        {
            "invoice_path": str(path),
            "extracted": None,
            "validation": None,
            "approval": None,
            "payment_status": None,
            "logs": [_ts(f"Starting pipeline for {path.name}")],
        }
    )
    result = PipelineResult(invoice_path=str(path))
    result.extracted = final.get("extracted")
    result.validation = final.get("validation")
    result.approval = final.get("approval")
    result.payment_status = final.get("payment_status")
    result.logs = final.get("logs", [])
    for line in result.logs:
        logger.debug(line, extra={"stage": "pipeline"})
    return result
