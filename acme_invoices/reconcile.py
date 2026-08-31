"""Agentic duplicate reconciliation for submissions sharing an invoice number."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .llm import LLMClient, parse_json_response, summarize_error
from .models import ExtractedInvoice, ReconciliationResult
from .review import request_manual_review

logger = logging.getLogger(__name__)

_RECON_RELATIONSHIPS = {"EXACT_DUPLICATE", "SUPERSEDES", "CONFLICT"}


def reconcile_duplicates(
    number: str,
    versions: list[tuple[Path, ExtractedInvoice]],
    db_path: Path,
    llm: Optional[LLMClient] = None,
) -> ReconciliationResult:
    """Compare submissions sharing one invoice number and decide how they relate.

    - EXACT_DUPLICATE: economically identical → pay once, drop the rest.
    - SUPERSEDES: one is a legitimate revision → process the authoritative version.
    - CONFLICT: materially inconsistent with no clear revision → escalate all.
    """
    payloads = [{"file": p.name, "invoice": asdict(ex)} for p, ex in versions]
    all_paths = [str(p) for p, _ in versions]
    by_name = {p.name: str(p) for p, _ in versions}

    def _fallback() -> ReconciliationResult:
        # A revision usually adds line items, so keep the most complete version.
        keep = str(max(versions, key=lambda v: len(v[1].line_items))[0])
        return ReconciliationResult(
            "SUPERSEDES",
            [keep],
            [p for p in all_paths if p != keep],
            "Grok unavailable – kept the most complete version (most line items).",
        )

    if llm is None:
        result = _fallback()
    else:
        try:
            prompt = f"""You are an accounts-payable analyst. Multiple submissions share the SAME invoice number ({number}).
Compare them so we never pay the same obligation twice while still honoring a legitimate revision.

Classify the relationship as exactly one of:
- "EXACT_DUPLICATE": economically identical (same line items, quantities, and total). Pay once.
- "SUPERSEDES": one version is a legitimate revision/amendment of the other (e.g. a "revision"/"R1" marker, added line items, or notes describing an amendment). The newer / more complete version replaces the older one.
- "CONFLICT": same number but materially inconsistent with no clear revision (e.g. different vendor or contradictory totals). Do not auto-pay; escalate to a human.

Submissions (each has a file name and its parsed invoice):
{json.dumps(payloads, indent=2, default=str)}

Return strictly valid JSON:
{{
  "relationship": "EXACT_DUPLICATE" | "SUPERSEDES" | "CONFLICT",
  "authoritative_file": "<file name to process, or null for CONFLICT>",
  "reasoning": "Explain the comparison: which fields match or differ, and why this version is authoritative."
}}
"""
            content = llm.complete(prompt)
            parsed = parse_json_response(content)

            relationship = str(parsed.get("relationship", "CONFLICT")).strip().upper()
            if relationship not in _RECON_RELATIONSHIPS:
                relationship = "CONFLICT"
            reasoning = parsed.get("reasoning", content)
            auth = parsed.get("authoritative_file")

            if relationship == "CONFLICT" or auth not in by_name:
                result = ReconciliationResult(relationship, [], all_paths, reasoning)
            else:
                keep = by_name[auth]
                result = ReconciliationResult(
                    relationship, [keep], [p for p in all_paths if p != keep], reasoning
                )
        except Exception as e:
            logger.warning("Reconciliation unavailable (%s); using deterministic fallback.", summarize_error(e))
            result = _fallback()

    logger.info(
        "Reconciliation for %s: %s | process=%s | skip=%s",
        number,
        result.relationship,
        [Path(p).name for p in result.process_paths] or ["none (escalated)"],
        [Path(p).name for p in result.skip_paths] or ["none"],
        extra={"stage": "reconciliation", "invoice_number": number, "detail": result.reasoning},
    )

    if not result.process_paths:
        request_manual_review(
            invoice_number=number,
            source="reconciliation",
            reason=f"CONFLICT — versions materially inconsistent with no clear revision. {result.reasoning}",
            db_path=db_path,
        )
    return result
