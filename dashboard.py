"""Acme Invoice Ops — Streamlit dashboard.

A read-and-act surface over the SQLite database the CLI pipeline writes
(`inventory.db`). It surfaces the agentic pipeline's results as an AP worklist
and an auditor-friendly ledger:

  • Overview      — KPIs + outcome funnel + recent activity
  • Review Queue  — pending manual reviews with Approve / Reject actions
  • Ledger        — paid invoices, goods received vs. catalog reference
  • Audit / FX    — per-invoice currency, dated FX rate/source, USD conversion
  • Reference     — inventory & approved-vendor tables

The CLI stays the processing engine; this app only reads its tables and records
human decisions back into `review_queue` + a `review_actions` log.

Run:
    streamlit run dashboard.py
Populate the database first:  python main.py --invoice_dir=data/invoices
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import os

import pandas as pd
import streamlit as st

DB_PATH = Path(os.getenv("ACME_DB_PATH", Path(__file__).with_name("inventory.db")))

CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY", "$": "USD"}

STATUS_COLORS = {
    "pending": "#B8860B",
    "approved": "#2E7D32",
    "rejected": "#C62828",
    "success": "#2E7D32",
    "held": "#B8860B",
}

REVIEW_ACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS review_actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT,
    action         TEXT,
    note           TEXT,
    reviewer       TEXT,
    acted_at       TEXT
)
"""


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(REVIEW_ACTIONS_DDL)
    conn.commit()
    return conn


def _read(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    """Run a query, returning an empty frame if the table doesn't exist yet."""
    try:
        return pd.read_sql_query(sql, conn)
    except (pd.errors.DatabaseError, sqlite3.OperationalError):
        return pd.DataFrame()


@st.cache_data(ttl=5)
def load_data(db_mtime: float) -> dict[str, pd.DataFrame]:
    """Load every table the dashboard needs. `db_mtime` busts the cache."""
    with _connect() as conn:
        return {
            "paid": _read(conn, "SELECT * FROM paid_invoices ORDER BY paid_at DESC"),
            "received": _read(conn, "SELECT * FROM received_goods"),
            "queue": _read(conn, "SELECT rowid AS _rowid, * FROM review_queue"),
            "inventory": _read(conn, "SELECT * FROM inventory ORDER BY item"),
            "vendors": _read(conn, "SELECT * FROM vendors ORDER BY name"),
            "actions": _read(conn, "SELECT * FROM review_actions ORDER BY acted_at DESC"),
            "runs": _read(conn, "SELECT * FROM pipeline_runs ORDER BY run_at DESC"),
            "reconciliations": _read(conn, "SELECT * FROM reconciliations ORDER BY run_at DESC"),
        }


def fx_facts_for(runs: pd.DataFrame, invoice_number: str) -> dict | None:
    """Canonical FX provenance for an invoice from `pipeline_runs`, if persisted."""
    if runs.empty or "invoice_number" not in runs.columns:
        return None
    match = runs[runs["invoice_number"] == invoice_number]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


def record_decision(invoice_number: str, action: str, note: str, reviewer: str) -> None:
    """Persist a human decision: flip the queue row's status and log the action."""
    with _connect() as conn:
        conn.execute(
            "UPDATE review_queue SET status = ? WHERE invoice_number = ? AND status = 'pending'",
            (action, invoice_number),
        )
        conn.execute(
            "INSERT INTO review_actions (invoice_number, action, note, reviewer, acted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (invoice_number, action, note, reviewer, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    st.cache_data.clear()


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#555")
    return (
        f"<span style='background:{color};color:white;padding:2px 8px;"
        f"border-radius:10px;font-size:0.8em'>{status}</span>"
    )


@st.cache_data(ttl=60)
def detect_currency(invoice_path: str | None) -> str | None:
    """Sniff the invoice's currency from its source file (ISO code or symbol)."""
    if not invoice_path:
        return None
    try:
        text = Path(invoice_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r"\b(EUR|USD|GBP|CAD|AUD|JPY|CHF)\b", text, re.I)
    if m:
        return m.group(1).upper()
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    return None


def _render_currency_note(runs: pd.DataFrame, row: pd.Series) -> None:
    """Show a foreign-currency note, preferring canonical FX facts."""
    facts = fx_facts_for(runs, row["invoice_number"])
    if facts and int(facts.get("fx_is_foreign") or 0):
        currency = facts.get("currency")
        rate = facts.get("fx_rate")
        rate_date = facts.get("fx_rate_date")
        source = facts.get("fx_source")
        native = facts.get("total_native")
        usd = facts.get("total_usd")
        line = f"🌍 Foreign-currency invoice (**{currency}**) — converted to USD"
        if native is not None and usd is not None:
            line += f": {currency} {native:,.2f} → ${usd:,.2f}"
        if rate is not None:
            line += f" at rate {rate:g}"
        if rate_date:
            line += f" as of {rate_date}"
        if source:
            line += f" ({source})"
        line += ". An FX tolerance buffer was applied during validation, so any remaining variance is beyond the FX band."
        st.info(line)
        return
    if facts is not None:
        return
    currency = detect_currency(row.get("invoice_path"))
    if currency and currency != "USD":
        st.info(
            f"🌍 Foreign-currency invoice (**{currency}**) — amounts were converted to USD "
            f"at the dated FX rate as of the invoice date, and an FX tolerance buffer was "
            f"applied during validation. Any remaining variance is beyond the FX band."
        )


def kpi_row(data: dict[str, pd.DataFrame]) -> None:
    paid, queue, received = data["paid"], data["queue"], data["received"]
    pending = queue[queue["status"] == "pending"] if not queue.empty else queue
    resolved = queue[queue["status"] != "pending"] if not queue.empty else queue

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Invoices paid", len(paid))
    c2.metric("Total paid", f"${paid['amount'].sum():,.0f}" if not paid.empty else "$0")
    c3.metric("Pending review", len(pending))
    c4.metric("Reviews resolved", len(resolved))
    c5.metric("Units received", int(received["quantity"].sum()) if not received.empty else 0)


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
def tab_overview(data: dict[str, pd.DataFrame]) -> None:
    st.subheader("Pipeline overview")
    kpi_row(data)

    paid, queue = data["paid"], data["queue"]
    pending = len(queue[queue["status"] == "pending"]) if not queue.empty else 0
    resolved = len(queue[queue["status"] != "pending"]) if not queue.empty else 0

    st.markdown("#### Outcomes")
    runs = data["runs"]
    if not runs.empty and "payment_status" in runs.columns:
        counts = runs["payment_status"].fillna("unknown").value_counts()
        funnel = counts.rename_axis("outcome").reset_index(name="count").set_index("outcome")
        st.bar_chart(funnel, horizontal=True)
        st.caption("Every processed invoice's final status, from the persisted `pipeline_runs` table.")
    else:
        funnel = pd.DataFrame(
            {"outcome": ["Paid", "In review", "Reviewed"], "count": [len(paid), pending, resolved]}
        ).set_index("outcome")
        st.bar_chart(funnel, horizontal=True)
        st.caption("Run the CLI to populate `pipeline_runs` and complete this funnel.")

    st.markdown("#### Recent activity")
    events: list[dict] = []
    for _, r in paid.head(20).iterrows():
        events.append(
            {"when": r["paid_at"], "event": f"Paid ${r['amount']:,.2f} → {r['vendor']}", "invoice": r["invoice_number"]}
        )
    if not data["actions"].empty:
        for _, r in data["actions"].head(20).iterrows():
            events.append(
                {"when": r["acted_at"], "event": f"Reviewer {r['action']}", "invoice": r["invoice_number"]}
            )
    if events:
        feed = pd.DataFrame(events).sort_values("when", ascending=False)
        st.dataframe(feed, hide_index=True, use_container_width=True)
    else:
        st.info("No activity yet. Run the CLI to populate the database.")


def tab_review_queue(data: dict[str, pd.DataFrame], reviewer: str) -> None:
    st.subheader("Manual review queue")
    queue = data["queue"]
    if queue.empty:
        st.info("No `review_queue` table found. Run the CLI first.")
        return

    show_resolved = st.toggle("Show resolved items", value=False)
    view = queue if show_resolved else queue[queue["status"] == "pending"]
    if view.empty:
        st.success("Queue is clear — nothing pending.")
        return

    for _, row in view.iterrows():
        header = f"{row['invoice_number']}  ·  raised by {row['source']}"
        with st.expander(header, expanded=(row["status"] == "pending")):
            st.markdown(status_badge(row["status"]), unsafe_allow_html=True)
            _render_currency_note(data["runs"], row)
            st.write(f"**Reason:** {row['reason']}")
            st.caption(f"Requested at {row['requested_at']}  ·  source: {row.get('invoice_path') or '—'}")

            if row["status"] == "pending":
                note = st.text_input(
                    "Reviewer note", key=f"note_{row['_rowid']}",
                    placeholder="Why are you approving / rejecting?",
                )
                a, b, _ = st.columns([1, 1, 4])
                if a.button("Approve", key=f"approve_{row['_rowid']}", type="primary"):
                    record_decision(row["invoice_number"], "approved", note, reviewer)
                    st.rerun()
                if b.button("Reject", key=f"reject_{row['_rowid']}"):
                    record_decision(row["invoice_number"], "rejected", note, reviewer)
                    st.rerun()


def tab_ledger(data: dict[str, pd.DataFrame]) -> None:
    st.subheader("Paid ledger")
    paid = data["paid"]
    if paid.empty:
        st.info("No paid invoices yet.")
    else:
        st.dataframe(paid, hide_index=True, use_container_width=True)
        st.markdown("**By vendor**")
        by_vendor = (
            paid.groupby("vendor")["amount"].agg(["count", "sum"])
            .rename(columns={"count": "invoices", "sum": "total_paid"})
            .sort_values("total_paid", ascending=False)
        )
        st.dataframe(by_vendor, use_container_width=True)

    st.subheader("Goods received vs. catalog reference")
    received, inventory = data["received"], data["inventory"]
    if received.empty or inventory.empty:
        st.info("No receipts recorded yet.")
        return
    agg = (
        received.groupby("item")["quantity"].sum().reset_index().rename(columns={"quantity": "received"})
    )
    catalog = inventory[["item", "stock"]].rename(columns={"stock": "catalog_stock"})
    position = catalog.merge(agg, on="item", how="outer").fillna({"received": 0})
    position["received"] = position["received"].astype(int)
    st.dataframe(position, hide_index=True, use_container_width=True)


def tab_reference(data: dict[str, pd.DataFrame]) -> None:
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Inventory")
        st.dataframe(data["inventory"], hide_index=True, use_container_width=True)
    with col2:
        st.subheader("Approved vendors")
        st.dataframe(data["vendors"], hide_index=True, use_container_width=True)


def tab_audit(data: dict[str, pd.DataFrame]) -> None:
    st.subheader("Audit & FX provenance")
    runs = data["runs"]
    if runs.empty:
        st.info("No `pipeline_runs` table found. Run the CLI to record FX provenance.")
        return

    foreign = runs[runs["fx_is_foreign"] == 1] if "fx_is_foreign" in runs.columns else pd.DataFrame()
    c1, c2, c3 = st.columns(3)
    c1.metric("Invoices processed", len(runs))
    c2.metric("Foreign-currency", len(foreign))
    if "total_usd" in runs.columns:
        c3.metric("Total (USD)", f"${runs['total_usd'].fillna(0).sum():,.0f}")

    if not foreign.empty:
        st.markdown("#### Foreign-currency conversions")
        cols = [
            c for c in [
                "invoice_number", "vendor", "invoice_date", "currency", "total_native",
                "fx_rate", "fx_rate_date", "fx_source", "total_usd", "payment_status",
            ] if c in foreign.columns
        ]
        st.dataframe(foreign[cols], hide_index=True, use_container_width=True)
        st.caption(
            "USD amounts use the audited FX rate as of each invoice's date. "
            "Non-USD invoices also receive an FX tolerance buffer during price validation."
        )

    st.markdown("#### All runs")
    hide = {"result_json", "logs"}
    display_cols = [c for c in runs.columns if c not in hide]
    st.dataframe(runs[display_cols], hide_index=True, use_container_width=True)

    recons = data.get("reconciliations", pd.DataFrame())
    if not recons.empty:
        st.markdown("#### Duplicate reconciliations")
        st.caption(
            "When an invoice number arrives more than once, the reconciliation agent decides "
            "how the versions relate. CONFLICTs are held for a human and surface here."
        )
        rcols = [
            c for c in ["invoice_number", "relationship", "process_paths", "skip_paths", "reasoning"]
            if c in recons.columns
        ]
        st.dataframe(recons[rcols], hide_index=True, use_container_width=True)

    st.markdown("#### Inspect a run")
    choice = st.selectbox("Invoice", runs["invoice_number"].dropna().unique())
    if choice:
        record = runs[runs["invoice_number"] == choice].iloc[0]
        detail, trail = st.columns(2)
        with detail:
            st.markdown("**Decision & payment**")
            st.write({
                "decision": record.get("decision"),
                "payment_status": record.get("payment_status"),
                "payment_reason": record.get("payment_reason"),
                "validation_passed": bool(record.get("validation_passed")),
            })
            flags = record.get("flags")
            if isinstance(flags, str) and flags not in ("", "[]"):
                try:
                    st.markdown("**Validation flags**")
                    st.write(json.loads(flags))
                except json.JSONDecodeError:
                    st.write(flags)
        with trail:
            st.markdown("**Run log**")
            logs = record.get("logs")
            if isinstance(logs, str) and logs:
                try:
                    st.code("\n".join(json.loads(logs)) or "(no log lines)")
                except json.JSONDecodeError:
                    st.code(logs)

        if not recons.empty and "invoice_number" in recons.columns:
            match = recons[recons["invoice_number"] == choice]
            if not match.empty:
                r = match.iloc[0]
                st.markdown("**Duplicate reconciliation**")
                st.write({"relationship": r.get("relationship"), "reasoning": r.get("reasoning")})


# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="Acme Invoice Ops", page_icon="🧾", layout="wide")
    st.title("🧾 Acme Invoice Ops")

    if not DB_PATH.exists():
        st.error(f"Database not found at {DB_PATH}. Run the CLI first: python main.py --invoice_dir=data/invoices")
        st.stop()

    with st.sidebar:
        st.header("Session")
        reviewer = st.text_input("Reviewer name", value="analyst")
        if st.button("Refresh data"):
            st.cache_data.clear()
        st.caption(f"DB: {DB_PATH.name}")

    data = load_data(DB_PATH.stat().st_mtime)

    overview, queue, ledger, audit, reference = st.tabs(
        ["Overview", "Review Queue", "Ledger", "Audit / FX", "Reference"]
    )
    with overview:
        tab_overview(data)
    with queue:
        tab_review_queue(data, reviewer)
    with ledger:
        tab_ledger(data)
    with audit:
        tab_audit(data)
    with reference:
        tab_reference(data)

    st.divider()
    st.caption(
        "Read-and-act surface over the CLI's SQLite tables. The **Audit / FX** tab renders "
        "per-invoice provenance — currency, dated FX rate/date/source, USD conversion, decision, "
        "and run log — from the `pipeline_runs` table the CLI persists."
    )


if __name__ == "__main__":
    main()
