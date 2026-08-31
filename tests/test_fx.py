"""FX dating and USD conversion."""
from __future__ import annotations

from acme_invoices.fx import convert_to_usd, fx_rate_asof


def test_latest_quote_when_no_date():
    fx = fx_rate_asof("EUR")
    assert fx.rate == 1.12  # latest EUR quote (2026-04-01)


def test_dated_quote_picks_latest_on_or_before():
    fx = fx_rate_asof("EUR", "2026-01-26")
    assert fx.rate == 1.16
    assert fx.as_of == "2026-01-01"
    assert "ECB" in fx.source


def test_quote_before_that_date():
    fx = fx_rate_asof("EUR", "2025-11-15")
    assert fx.rate == 1.10


def test_predates_table_uses_earliest():
    fx = fx_rate_asof("EUR", "2020-01-01")
    assert fx.rate == 1.10
    assert "earliest available" in fx.source


def test_unknown_currency_falls_back_to_one():
    fx = fx_rate_asof("XYZ", "2026-01-01")
    assert fx.rate == 1.0
    assert "fallback" in fx.source


def test_convert_to_usd_uses_dated_rate():
    assert convert_to_usd(100.0, "EUR", "2026-01-26") == 116.0
    assert convert_to_usd(None, "EUR") == 0.0
    assert convert_to_usd(50.0, "USD") == 50.0
