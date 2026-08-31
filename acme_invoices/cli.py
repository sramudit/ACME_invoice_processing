"""Command-line interface for the Acme invoice-processing pipeline.

Examples
--------
    python main.py --invoice_path=data/invoices/invoice_1004.json
    python main.py --invoice_dir=data/invoices --log-format=json --out=results.json
    python main.py --invoice_path=data/invoices/invoice_1002.txt --offline --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import PROJECT_ROOT, load_settings
from .llm import get_llm
from .logging_config import configure_logging
from .persistence import result_to_document, setup_inventory
from .pipeline import persist_results, process_folder, process_single

logger = logging.getLogger("acme_invoices.cli")

_STATUS_ICON = {
    "success": "[PAID]",
    "held": "[HELD]",
    "rejected": "[REJECTED]",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acme-invoices",
        description="Automate invoice ingestion, validation, approval, and payment.",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--invoice_path", type=Path, help="Process a single invoice file.")
    src.add_argument(
        "--invoice_dir",
        type=Path,
        help="Process every supported invoice in a folder (with duplicate reconciliation).",
    )
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path (default: inventory.db).")
    parser.add_argument("--reset-db", action="store_true", help="Recreate and reseed the database before running.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force deterministic agents (never call Grok), even if XAI_API_KEY is set.",
    )
    parser.add_argument("--model", default=None, help="Grok model name (default: grok-3).")
    parser.add_argument(
        "--log-format", choices=["rich", "json"], default="rich", help="Console log format."
    )
    parser.add_argument("--log-file", default=None, help="Also write JSON-lines events to this file.")
    parser.add_argument("--out", type=Path, default=None, help="Write the result document(s) as JSON here.")
    parser.add_argument("--no-persist", action="store_true", help="Skip writing provenance tables to the DB.")
    dash = parser.add_mutually_exclusive_group()
    dash.add_argument(
        "--dashboard",
        dest="dashboard",
        action="store_true",
        default=None,
        help="Launch the Streamlit dashboard after the run (default when Grok is active).",
    )
    dash.add_argument(
        "--no-dashboard",
        dest="dashboard",
        action="store_false",
        help="Never launch the Streamlit dashboard.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")
    return parser


def _print_summary(results) -> None:
    for r in results:
        status = (r.payment_status or {}).get("status", "-")
        icon = _STATUS_ICON.get(status, "[?]")
        inv = r.extracted.invoice_number if r.extracted else Path(r.invoice_path).name
        decision = r.approval.decision.value if r.approval else "-"
        total = (
            f"${r.extracted.total:,.2f}"
            if r.extracted and r.extracted.total is not None
            else "-"
        )
        logger.info("%s %s | decision=%s | total=%s | status=%s", icon, inv, decision, total, status)


def _launch_dashboard(settings) -> None:
    """Open the Streamlit dashboard against the run's database (blocks until closed)."""
    dashboard = PROJECT_ROOT / "dashboard.py"
    if not dashboard.exists():
        logger.warning("Dashboard not found at %s – skipping launch.", dashboard)
        return
    env = {**os.environ, "ACME_DB_PATH": str(settings.db_path.resolve())}
    logger.info("Launching Streamlit dashboard (Ctrl+C to stop)…")
    try:
        subprocess.run(
            [sys.executable, "-m", "streamlit", "run", str(dashboard)],
            env=env,
            check=False,
        )
    except FileNotFoundError:
        logger.error("Streamlit is not installed. Install it with: pip install streamlit")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(log_format=args.log_format, log_file=args.log_file, verbose=args.verbose)

    settings = load_settings(
        db_path=args.db,
        data_dir=args.invoice_dir,
        offline=args.offline,
        model=args.model,
    )

    if args.reset_db or not settings.db_path.exists():
        setup_inventory(settings.db_path, reset=args.reset_db)
        logger.info("Inventory database ready at %s", settings.db_path)

    llm = get_llm(offline=settings.offline, api_key=settings.api_key, model=settings.model)
    if settings.offline:
        logger.info("xAI API key: ignored (--offline). Using offline deterministic agents.")
    elif settings.api_key:
        masked = f"…{settings.api_key[-4:]}" if len(settings.api_key) >= 4 else "set"
        logger.info("xAI API key: found (%s). Using Grok model '%s'.", masked, settings.model)
    else:
        logger.info(
            "xAI API key: not found (set XAI_API_KEY or use a .env file). "
            "Using offline deterministic agents."
        )

    if args.invoice_path:
        invoice_path = args.invoice_path
        if not invoice_path.is_absolute() and not invoice_path.exists():
            invoice_path = PROJECT_ROOT / invoice_path
        if not invoice_path.exists():
            logger.error("Invoice not found: %s", args.invoice_path)
            return 2
        results = [process_single(invoice_path, settings, llm)]
    else:
        if not settings.data_dir.exists():
            logger.error("Invoice directory not found: %s", settings.data_dir)
            return 2
        results = process_folder(settings, llm)

    if not args.no_persist:
        runs, recons = persist_results(results, settings)
        logger.info("Persisted %d run(s) and %d reconciliation verdict(s).", runs, recons)

    _print_summary(results)

    documents = [result_to_document(r) for r in results]
    if args.out:
        args.out.write_text(json.dumps(documents, indent=2, default=str), encoding="utf-8")
        logger.info("Wrote %d result document(s) to %s", len(documents), args.out)
    elif args.log_format == "json":
        print(json.dumps(documents, indent=2, default=str))

    hard_failures = sum(
        1 for r in results if r.extracted is None and not r.invoice_path.startswith("(reconciliation:")
    )

    # Auto-launch the dashboard when Grok is active, unless explicitly disabled.
    launch = args.dashboard if args.dashboard is not None else settings.use_grok
    if launch:
        if args.no_persist:
            logger.warning("--no-persist was set: the dashboard will show prior data, not this run.")
        _launch_dashboard(settings)

    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
