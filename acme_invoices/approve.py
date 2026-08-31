"""Approval: deterministic rules + a Grok reflection loop, plus an agentic
price-variance context review before escalating to a human."""
from __future__ import annotations

import logging
from pathlib import Path
from dataclasses import asdict
from typing import Optional

from .config import HIGH_VALUE_THRESHOLD
from .fx import convert_to_usd, fx_rate_asof
from .ingest import read_source_text
from .llm import LLMClient, parse_json_response, summarize_error
from .models import (
    ApprovalResult,
    ContextReviewResult,
    Decision,
    ExtractedInvoice,
    ValidationResult,
)

logger = logging.getLogger(__name__)

# Grok answers with verbs (APPROVE/REJECT/...) that don't match the enum member
# names; map them explicitly so a clean "APPROVE" is not silently downgraded.
_GROK_DECISION_MAP = {
    "APPROVE": Decision.APPROVED,
    "APPROVED": Decision.APPROVED,
    "REJECT": Decision.REJECTED,
    "REJECTED": Decision.REJECTED,
    "NEEDS_REVIEW": Decision.NEEDS_REVIEW,
    "NEEDS REVIEW": Decision.NEEDS_REVIEW,
    "REVIEW": Decision.NEEDS_REVIEW,
}

_CONTEXT_VERDICTS = {"JUSTIFIED", "UNJUSTIFIED", "INSUFFICIENT_CONTEXT"}
_VARIANCE_JUSTIFIERS = (
    "rush", "expedite", "priority", "urgent", "custom", "small batch",
    "small-batch", "freight", "surcharge", "premium", "special order", "overtime",
)


def _simple_critique(
    extracted: ExtractedInvoice, validation: ValidationResult, first_decision: Decision
) -> str:
    """Deterministic stand-in for an LLM critique / reflection step."""
    notes = []
    if not validation.passed:
        notes.append("Validation already failed – rejection is correct.")
    if extracted.total and extracted.total > HIGH_VALUE_THRESHOLD:
        notes.append(f"High-value invoice (${extracted.total:,.2f}) – extra caution warranted.")
    if extracted.anomalies:
        notes.append(f"Anomalies present: {extracted.anomalies}")
    if first_decision == Decision.APPROVED and not notes:
        notes.append("No red flags on second look. Approval stands.")
    return " | ".join(notes) if notes else "No additional concerns."


def _offline_critique_decision(
    extracted: ExtractedInvoice, validation: ValidationResult
) -> Decision:
    """Deterministic critique verdict mirroring the rule controls so the
    offline reflection step doesn't silently upgrade an escalation to APPROVE."""
    if not validation.passed:
        return Decision.REJECTED
    total_usd = convert_to_usd(extracted.total, extracted.currency, extracted.invoice_date)
    if total_usd and total_usd > HIGH_VALUE_THRESHOLD:
        return Decision.NEEDS_REVIEW
    return Decision.APPROVED


def grok_critique(
    extracted: ExtractedInvoice,
    validation: ValidationResult,
    llm: Optional[LLMClient] = None,
) -> dict:
    """Return Grok's decision + reasoning. Falls back to a deterministic critique
    when no LLM client is supplied or the call fails."""
    if llm is None:
        decision = _offline_critique_decision(extracted, validation)
        return {
            "decision": decision,
            "reasoning": _simple_critique(extracted, validation, decision),
        }

    try:
        prompt = f"""You are a meticulous VP of Finance reviewing an invoice for final approval before payment.

        Review the provided invoice data and validation flags against these corporate controls:

        1. APPROVE IF:
            - All deterministic validation checks passed without flags.
            - Total amount is equal to or under $10,000 USD.
            - The vendor is on the pre-approved vendor list and active.
            - Item pricing matches catalog prices within acceptable tolerance limits.

        2. ESCALATE / MARK AS NEEDS_REVIEW IF:
            - High-Value: The total invoice amount exceeds $10,000 USD (requires elevated VP scrutiny even if all validation checks pass).
            - Unexpected invoice revisions (e.g., "R1" revisions) or duplicate invoices that may indicate potential fraud or miscommunication.
            - Minor Discrepancies: Small subtotal/total calculation variances or unverified note additions that do not indicate immediate fraud.

        3. REJECT IF:
            - Validation Failed: Contains validation flags such as STOCK_MISMATCH, UNKNOWN_ITEM, OUT_OF_STOCK (0 stock catalog items), or NEGATIVE_QTY/TOTAL.
            - Fraud / Security Risks: Unknown or blocked vendors, urgent wire transfer requests, or duplicate invoice submissions.

        You have the following information available:

        Invoice data:
        {asdict(extracted)}

        Validation flags:
        {validation.flags}

        Return your response strictly in valid JSON format with the following keys:
        {{
        "decision": "APPROVE" | "REJECT" | "NEEDS_REVIEW",
        "reasoning": "A detailed justification. Walk through each relevant control (validation flags, total vs. $10,000 threshold, vendor status, pricing, revisions/duplicates) and state explicitly why it does or does not trigger escalation or rejection."
        }}
        """
        content = llm.complete(prompt)
        parsed = parse_json_response(content)

        decision_str = str(parsed.get("decision", "NEEDS_REVIEW")).strip().upper()
        decision_enum = _GROK_DECISION_MAP.get(decision_str, Decision.NEEDS_REVIEW)
        if decision_str not in _GROK_DECISION_MAP:
            logger.warning(
                "Unrecognized Grok decision %r – defaulting to NEEDS_REVIEW", decision_str
            )
        reasoning = parsed.get("reasoning", content)
        return {"decision": decision_enum, "reasoning": reasoning}
    except Exception as e:
        logger.warning("Grok critique unavailable (%s); using deterministic fallback.", summarize_error(e))
        decision = _offline_critique_decision(extracted, validation)
        return {
            "decision": decision,
            "reasoning": _simple_critique(extracted, validation, decision),
        }


def approve_invoice_base(
    extracted: ExtractedInvoice,
    validation: ValidationResult,
    llm: Optional[LLMClient] = None,
    max_critiques: int = 1,
) -> ApprovalResult:
    """Raw rules + Grok reflection loop (no price-variance context review)."""
    history: list[str] = []
    total_usd = convert_to_usd(extracted.total, extracted.currency, extracted.invoice_date)

    if not validation.passed:
        decision = Decision.REJECTED
        reason = "Validation failed: " + "; ".join(validation.flags)
        scrutiny = "elevated"
    elif total_usd > HIGH_VALUE_THRESHOLD:
        decision = Decision.NEEDS_REVIEW
        reason = f"Amount {extracted.currency} {extracted.total:,.2f} (${total_usd:,.2f} USD) exceeds threshold"
        scrutiny = "elevated"
    else:
        decision = Decision.APPROVED
        reason = "All validation checks passed"
        scrutiny = "standard"

    history.append(f"Initial Rule Check: {decision.value} – {reason}")

    for i in range(max_critiques):
        critique = grok_critique(extracted, validation, llm)
        grok_decision = critique["decision"]
        grok_reasoning = critique["reasoning"]
        history.append(f"Grok Critique {i + 1}: Decision={grok_decision.value} | {grok_reasoning}")

        if decision != Decision.REJECTED:
            if grok_decision != decision:
                reason = f"Grok decision ({grok_decision.value}): {grok_reasoning}"
                decision = grok_decision
            else:
                reason = f"Approved via Grok critique: {grok_reasoning}"

    return ApprovalResult(
        decision=decision, reason=reason, scrutiny_level=scrutiny, critique_history=history
    )


def only_price_variance(flags: list[str]) -> bool:
    """True when every validation flag is a price variance (nothing harder)."""
    return bool(flags) and all(f.startswith("PRICE_VARIANCE") for f in flags)


def request_context_review(
    extracted: ExtractedInvoice,
    variance_flags: list[str],
    invoice_path: Optional[str | Path] = None,
    llm: Optional[LLMClient] = None,
) -> ContextReviewResult:
    """Decide whether a price variance is justified by documented commercial terms
    (rush/custom/freight/overtime) before escalating. Currency drift is NOT a
    justification here — it is already absorbed by the FX-adjusted tolerance."""
    doc_text = ""
    if invoice_path:
        try:
            doc_text = read_source_text(Path(invoice_path))
        except Exception:
            doc_text = ""
    item_notes = "; ".join(
        f"{li.item}: {li.note}" for li in extracted.line_items if getattr(li, "note", None)
    )

    currency = (extracted.currency or "USD").upper()
    fx = fx_rate_asof(extracted.currency, extracted.invoice_date) if currency != "USD" else None
    fx_facts = ""
    if fx is not None:
        conversions = "; ".join(
            f"{li.item}: {currency} {li.unit_price:.2f} = ${round(li.unit_price * fx.rate, 2):.2f} USD"
            for li in extracted.line_items
            if li.unit_price
        )
        fx_facts = (
            f"FX CONTEXT (transparency only): invoice is denominated in {currency}, converted to USD at the "
            f"audited rate {fx.rate:g} as of {fx.as_of} ({fx.source}). Validation already granted an FX buffer, "
            f"so this variance exceeds the FX-adjusted tolerance and is NOT explained by currency movement alone. "
            f"Conversions — {conversions}."
        )

    context_blob = "\n".join(
        filter(None, [doc_text, extracted.raw_notes, "; ".join(extracted.anomalies), item_notes, fx_facts])
    )
    flags_text = "\n".join(variance_flags)

    def _fallback() -> ContextReviewResult:
        hay = context_blob.lower()
        if any(k in hay for k in _VARIANCE_JUSTIFIERS):
            return ContextReviewResult(
                "JUSTIFIED",
                "Invoice context references a rush/expedite/custom arrangement that "
                "explains the higher unit price.",
            )
        return ContextReviewResult(
            "INSUFFICIENT_CONTEXT",
            "No documented commercial terms (rush/expedite/custom/freight/overtime) justify "
            "the variance; currency drift is already covered by the FX-adjusted tolerance, so "
            "escalate for human review.",
        )

    if llm is None:
        return _fallback()

    try:
        prompt = f"""You are an accounts-payable analyst deciding whether a PRICE VARIANCE needs a human approver.
A line item is billed above its catalog price. Read the invoice's OWN context and decide whether the premium is
legitimately explained by documented commercial terms: a rush/expedite order, custom or small-batch pricing,
freight/surcharge, overtime, or a contractual premium.

IMPORTANT: Do NOT justify the variance on currency/FX grounds. Validation already applied an FX-adjusted tolerance
(base price tolerance plus a currency buffer); any variance that reached you is BEYOND that band, so it is not
explained by exchange-rate movement. The dated FX facts below are provided for transparency only. If there is no
documented commercial reason, return UNJUSTIFIED (or INSUFFICIENT_CONTEXT) so a human can review.

Price-variance flags:
{flags_text}

Invoice context (source document text + parsed notes/anomalies + dated FX facts):
{context_blob[:4000]}

Return strictly valid JSON:
{{
  "verdict": "JUSTIFIED" | "UNJUSTIFIED" | "INSUFFICIENT_CONTEXT",
  "reasoning": "Cite the specific documented terms that do or do not explain the variance (not FX)."
}}
"""
        content = llm.complete(prompt)
        parsed = parse_json_response(content)
        verdict = str(parsed.get("verdict", "INSUFFICIENT_CONTEXT")).strip().upper()
        if verdict not in _CONTEXT_VERDICTS:
            verdict = "INSUFFICIENT_CONTEXT"
        return ContextReviewResult(verdict, parsed.get("reasoning", content))
    except Exception as e:
        logger.warning("Context review unavailable (%s); using deterministic fallback.", summarize_error(e))
        return _fallback()


def resolve_price_variance(
    extracted: ExtractedInvoice,
    validation: ValidationResult,
    invoice_path: Optional[str | Path] = None,
    llm: Optional[LLMClient] = None,
) -> ApprovalResult:
    """Run a context review on a price-variance-only invoice and turn the verdict
    into an approval: approve if justified and under threshold, else escalate."""
    review = request_context_review(extracted, validation.flags, invoice_path, llm)
    total_usd = convert_to_usd(extracted.total, extracted.currency, extracted.invoice_date)
    history = [
        f"Initial Rule Check: price variance flagged – {'; '.join(validation.flags)}",
        f"Context review ({review.verdict}): {review.reasoning}",
    ]
    if review.verdict == "JUSTIFIED" and total_usd <= HIGH_VALUE_THRESHOLD:
        return ApprovalResult(
            Decision.APPROVED,
            f"Price variance justified by invoice context: {review.reasoning}",
            "elevated",
            history,
        )
    if review.verdict == "JUSTIFIED":
        return ApprovalResult(
            Decision.NEEDS_REVIEW,
            f"Variance justified by context but high-value — VP sign-off required: {review.reasoning}",
            "elevated",
            history,
        )
    return ApprovalResult(
        Decision.NEEDS_REVIEW,
        f"Price variance unresolved after context review ({review.verdict}): {review.reasoning}",
        "elevated",
        history,
    )


def approve_invoice(
    extracted: ExtractedInvoice,
    validation: ValidationResult,
    invoice_path: Optional[str | Path] = None,
    llm: Optional[LLMClient] = None,
    max_critiques: int = 1,
) -> ApprovalResult:
    """Full approval for direct (non-graph) calls: self-resolves a price-variance-
    only failure via a context review, otherwise runs the rules + Grok loop."""
    if not validation.passed and only_price_variance(validation.flags):
        return resolve_price_variance(extracted, validation, invoice_path, llm)
    return approve_invoice_base(extracted, validation, llm, max_critiques=max_critiques)
