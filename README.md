# Acme Invoice Processing

An agentic, LangGraph-orchestrated invoice-processing pipeline for the Acme /
Galatiq case, packaged as a runnable CLI. It ingests invoices in multiple
formats, validates them against inventory and vendor controls, runs an
LLM-assisted approval + reflection loop, reconciles duplicate/revised invoices,
and executes (mock) payment or escalates to a human review queue.

This is designed to run **fully offline and deterministically** (no network, no API key required) via a
built-in mock LLM, while transparently using xAI Grok when an API key is present.

## Run with Grok + Streamlit (quickest path)

Set your xAI Grok API key, then run the pipeline — the Streamlit dashboard opens
automatically when the run finishes:

```bash
cd ACME_invoice_processing
pip install -e ".[dev]"

export XAI_API_KEY=xai-your-key-here          # or: cp .env.example .env and edit it

python main.py --invoice_dir=data/invoices --reset-db
```

The run uses Grok for approval/reconciliation and then launches the dashboard against
this run's ledger. Add `--no-dashboard` to skip the launch. See
[Adding your xAI Grok API key](#adding-your-xai-grok-api-key) for details.

## Features

- **Multi-format ingest**: JSON, CSV, XML, plain text, and PDF invoices.
- **Deterministic validation**: duplicate detection, vendor allow-list, stock and
  catalog checks, price-variance (with FX-adjusted tolerance), tax and total
  reconciliation, negative-quantity/total guards.
- **Agentic approval**: rule-based decision + an LLM critique/reflection loop, plus
  an automated price-variance *context review* before escalating to a human.
- **Dated FX**: as-of currency conversion with an auditable provenance trail.
- **Duplicate reconciliation**: SUPERSEDES / CONFLICT resolution for revised invoices.
- **Persistence**: SQLite ledger (inventory, vendors, paid invoices, review queue,
  pipeline runs, reconciliation verdicts).
- **Observability**: structured JSON-lines logs (CI-friendly) or Rich console output.
- **Streamlit dashboard** for reviewing results, the review queue, and FX audit trails.

## Quickstart

Requires Python 3.11+.

```bash
cd grok/ACME_invoice_processing

# Install (editable); add [dev] for the test tooling
pip install -e ".[dev]"

# Process the sample invoice set, offline & deterministic
python main.py --invoice_dir=data/invoices --offline --reset-db
```

## CLI usage

```bash
# Single invoice
python main.py --invoice_path=data/invoices/invoice_1001.txt

# A folder of invoices, JSON logs, write results to a file
python main.py --invoice_dir=data/invoices --log-format=json --out=results.json

# Force offline deterministic mode and reset the ledger first
python main.py --invoice_dir=data/invoices --offline --reset-db --verbose
```

If installed, the console entry point `acme-invoices` is equivalent to `python main.py`.

When a Grok API key is active (see below), the Streamlit dashboard **launches
automatically** after the run. Use `--no-dashboard` to suppress it, or `--dashboard`
to force it (e.g. during an offline run).

### Options

| Flag | Description |
| --- | --- |
| `--invoice_path PATH` | Process a single invoice file. |
| `--invoice_dir DIR` | Process every supported invoice in a folder (mutually exclusive with `--invoice_path`). |
| `--db PATH` | SQLite database path (default: `inventory.db`). |
| `--reset-db` | Drop and reseed the database before running. |
| `--offline` | Force the deterministic mock LLM (no network). |
| `--model NAME` | Grok model to use when online (default: `grok-3`). |
| `--log-format {rich,json}` | Console log format (default: `rich`). |
| `--log-file PATH` | Also write JSON-lines logs to a file. |
| `--out PATH` | Write per-invoice result documents to a JSON file. |
| `--no-persist` | Skip writing pipeline runs / reconciliations to the DB. |
| `--dashboard` / `--no-dashboard` | Force / suppress the Streamlit dashboard after the run (default: launch when Grok is active). |
| `-v, --verbose` | Enable DEBUG-level logging. |

## Configuration (12-factor)

Configuration is read from the environment (or a local `.env` file):

- `XAI_API_KEY` — xAI Grok API key. When present (and `--offline` is not set), the
  pipeline uses Grok; otherwise it falls back to the deterministic mock LLM.
- `XAI_MODEL` — Grok model to use (default: `grok-3`; overridden by `--model`).

### Adding your xAI Grok API key

Get a key from the [xAI console](https://console.x.ai/) (starts with `xai-`), then
provide it in **one** of these ways:

**Option A — `.env` file (recommended for local dev).** Copy the template and fill in
your key. The file is git-ignored, and `load_settings` loads it automatically.

```bash
cp .env.example .env
# edit .env and set XAI_API_KEY=xai-...
```

**Option B — shell environment variable.**

```bash
export XAI_API_KEY=xai-your-key-here
```

Then run **without** `--offline` so Grok is actually called:

```bash
python main.py --invoice_dir=data/invoices --reset-db
```

With a key active, the pipeline runs through Grok and then **opens the Streamlit
dashboard automatically** so you can review the results. Add `--no-dashboard` to skip it.

> Note: `--offline` always wins — it ignores any key and forces the deterministic mock
> LLM. If the key is missing or the Grok API is unreachable, the pipeline logs a warning
> and cleanly falls back to deterministic logic, so runs never fail for lack of a key.


## Dashboard

When a Grok key is active, the dashboard launches automatically after each run. You can
also start it manually against the current ledger:

```bash
streamlit run dashboard.py
```

Point it at a specific database with the `ACME_DB_PATH` environment variable (the CLI
sets this automatically when it auto-launches). The dashboard exposes Overview, Review
Queue, Ledger, Audit/FX, and Reference tabs.

## Running tests

```bash
pip install -e ".[dev]"
python -m pytest                 # run the suite
python -m pytest --cov=acme_invoices --cov-report=term-missing
```

Tests are hermetic: the LLM is mocked and the pipeline runs offline. Current
coverage is ~92% overall (all modules ≥ 80%).

## Architecture

```
ingest → validate → approve ─┬─(context_review)→ pay
                             ├─→ manual_review (human queue)
                             └─→ reject
```

The pipeline is a LangGraph `StateGraph` (see `acme_invoices/graph.py`) whose nodes
are thin wrappers over pure, independently testable functions:

| Module | Responsibility |
| --- | --- |
| `config.py` | `Settings` + environment loading, thresholds, paths. |
| `models.py` | Typed dataclasses / enums for the pipeline state. |
| `ingest.py` | Format parsers, normalization, catalog canonicalization. |
| `validate.py` | Deterministic control checks and flags. |
| `approve.py` | Rule decision + LLM reflection loop + price-variance context review. |
| `fx.py` | Dated FX rates and USD conversion with provenance. |
| `llm.py` | `LLMClient` protocol, Grok client, and mock fallback. |
| `reconcile.py` | Duplicate/revision reconciliation. |
| `payment.py` | Mock payment execution and ledger writes. |
| `review.py` | Human review-queue escalation. |
| `graph.py` | LangGraph assembly and routing. |
| `pipeline.py` | Single-file / folder orchestration and persistence. |
| `persistence.py` | SQLite schema, seeding, and result serialization. |
| `logging_config.py` | JSON-lines / Rich structured logging. |
| `cli.py` | Argument parsing and the runnable entry point. |

## Design notes

Relative to the source notebook, this port removes teaching-time globals and
monkey-patching in favor of injected `Settings`, an injected `LLMClient`, and
explicit `db_path` parameters — making every stage pure and unit-testable, and
letting the whole pipeline run deterministically offline.
