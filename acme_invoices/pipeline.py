"""High-level orchestration: process one invoice or a whole folder, grouping
duplicate submissions for the reconciliation agent and persisting provenance."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Settings
from .graph import build_invoice_graph, run_pipeline
from .ingest import ingest_invoice, invoice_files
from .llm import LLMClient
from .models import ExtractedInvoice, PipelineResult
from .persistence import persist_pipeline_runs, persist_reconciliations
from .reconcile import reconcile_duplicates

logger = logging.getLogger(__name__)


def _ts(msg: str) -> str:
    return f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"


def process_single(path: Path, settings: Settings, llm: Optional[LLMClient] = None) -> PipelineResult:
    """Run one invoice through the graph (no cross-file reconciliation)."""
    graph = build_invoice_graph(settings, llm)
    return run_pipeline(path, graph)


def process_folder(
    settings: Settings, llm: Optional[LLMClient] = None
) -> list[PipelineResult]:
    """Ingest every supported invoice, reconcile duplicates by invoice number, and
    route the selected version(s) through the graph."""
    graph = build_invoice_graph(settings, llm)

    submissions: dict[str, list[tuple[Path, ExtractedInvoice]]] = defaultdict(list)
    for inv in invoice_files(settings.data_dir):
        ex = ingest_invoice(inv, settings.db_path)
        submissions[ex.invoice_number].append((Path(inv), ex))

    results: list[PipelineResult] = []
    for number, versions in submissions.items():
        recon = None
        if len(versions) > 1:
            recon = reconcile_duplicates(number, versions, settings.db_path, llm)
            process_paths = recon.process_paths
        else:
            process_paths = [str(versions[0][0])]

        for path in process_paths:
            r = run_pipeline(path, graph)
            if recon is not None:
                r.reconciliation = asdict(recon)
            results.append(r)

        # CONFLICT: nothing auto-processed. Record a held stub so the agent's
        # reasoning still reaches the JSON logs and the reconciliations table.
        if recon is not None and not recon.process_paths:
            stub = PipelineResult(invoice_path=f"(reconciliation:{number})")
            stub.reconciliation = asdict(recon)
            stub.payment_status = {
                "status": "held",
                "reason": f"CONFLICT — escalated to manual review: {recon.reasoning}",
            }
            stub.logs = [_ts(f"Reconciliation CONFLICT for {number}: {recon.reasoning}")]
            results.append(stub)

    return results


def persist_results(results: list[PipelineResult], settings: Settings) -> tuple[int, int]:
    """Persist run + reconciliation provenance for the dashboard/audit view."""
    runs = persist_pipeline_runs(results, settings.db_path)
    recons = persist_reconciliations(results, settings.db_path)
    return runs, recons
