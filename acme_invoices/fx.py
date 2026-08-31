"""Dated FX rate table with audit provenance.

Each currency maps to dated quotes (effective_date, rate_to_usd, source) sorted
ascending; ``fx_rate_asof`` returns the latest quote on or before the invoice
date, so every conversion is reproducible and carries a date + source.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Optional

from .models import FxRate

# Illustrative 30-day-average quotes. A real deployment would load these from a
# rates service; the point is that each conversion is dated and attributable.
FX_RATE_TABLE: dict[str, list[tuple[str, float, str]]] = {
    "USD": [("2020-01-01", 1.00, "base")],
    "EUR": [
        ("2025-10-01", 1.10, "ECB 30-day avg 2025-10"),
        ("2025-12-01", 1.13, "ECB 30-day avg 2025-12"),
        ("2026-01-01", 1.16, "ECB 30-day avg 2026-01"),
        ("2026-04-01", 1.12, "ECB 30-day avg 2026-04"),
    ],
    "GBP": [
        ("2025-10-01", 1.28, "BoE 30-day avg 2025-10"),
        ("2026-01-01", 1.31, "BoE 30-day avg 2026-01"),
    ],
    "CHF": [("2026-01-01", 1.17, "SNB 30-day avg 2026-01")],
    "CAD": [("2026-01-01", 0.73, "BoC 30-day avg 2026-01")],
    "AUD": [("2026-01-01", 0.66, "RBA 30-day avg 2026-01")],
    "JPY": [("2026-01-01", 0.0067, "BoJ 30-day avg 2026-01")],
}

# Back-compat alias: latest known rate per currency (for any undated caller).
FX_30_DAY_AVG_TO_USD = {cur: quotes[-1][1] for cur, quotes in FX_RATE_TABLE.items()}

# Extra tolerance granted to non-USD invoices so ordinary currency drift does
# not masquerade as a vendor pricing problem. Anything beyond this band is
# escalated for human review.
FX_TOLERANCE_BUFFER = 0.03


def fx_rate_asof(currency: Optional[str], as_of: Optional[str] = None) -> FxRate:
    """Latest dated quote on or before ``as_of`` (YYYY-MM-DD); latest known if no date."""
    cur = (currency or "USD").upper()
    quotes = FX_RATE_TABLE.get(cur)
    if not quotes:
        return FxRate(cur, 1.0, as_of or "n/a", "unknown currency — fallback 1.0")
    if not as_of:
        d, r, s = quotes[-1]
        return FxRate(cur, r, d, s)
    idx = bisect_right([q[0] for q in quotes], as_of) - 1
    if idx < 0:  # invoice predates the table
        d, r, s = quotes[0]
        return FxRate(cur, r, d, f"{s} (earliest available; predates invoice date)")
    d, r, s = quotes[idx]
    return FxRate(cur, r, d, s)


def convert_to_usd(
    amount: Optional[float], currency: Optional[str], as_of: Optional[str] = None
) -> float:
    """Convert a native amount to USD using the dated rate as of ``as_of``."""
    if amount is None:
        return 0.0
    return round(amount * fx_rate_asof(currency, as_of).rate, 2)
