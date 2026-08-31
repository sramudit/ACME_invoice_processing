"""Validation: check extracted invoices against the SQLite inventory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import PRICE_TOLERANCE
from .fx import FX_TOLERANCE_BUFFER, fx_rate_asof
from .models import ExtractedInvoice, ValidationResult


def validate_invoice(extracted: ExtractedInvoice, db_path: Path) -> ValidationResult:
    """Flag stock mismatches, unknown/zero-stock items, bad data, price variance,
    duplicate submissions, and vendor problems against the inventory database."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    flags: list[str] = []
    details: dict = {"item_checks": []}

    # Duplicate check: has this invoice number already been paid?
    cur.execute(
        "SELECT vendor, amount, paid_at FROM paid_invoices WHERE invoice_number = ?",
        (extracted.invoice_number,),
    )
    prior = cur.fetchone()
    if prior is not None:
        p_vendor, p_amount, p_paid_at = prior
        flags.append(
            f"DUPLICATE_INVOICE: {extracted.invoice_number} was already paid "
            f"({p_vendor}, ${p_amount:,.2f} on {p_paid_at})"
        )
        details["duplicate_of"] = {"vendor": p_vendor, "amount": p_amount, "paid_at": p_paid_at}

    # Vendor checks: blank name, or a name not on the approved list.
    vendor = (extracted.vendor or "").strip()
    if not vendor or vendor == "UNKNOWN":
        flags.append("MISSING_VENDOR: vendor name is blank or unknown")
    else:
        cur.execute("SELECT approved FROM vendors WHERE name = ?", (vendor,))
        vrow = cur.fetchone()
        if vrow is None:
            flags.append(f"UNKNOWN_VENDOR: '{vendor}' is not on the approved-vendor list")
        elif not vrow[0]:
            flags.append(f"BLOCKED_VENDOR: '{vendor}' is flagged as not approved")

    for li in extracted.line_items:
        check = {"item": li.item, "requested": li.quantity}

        if li.quantity < 0:
            flags.append(f"NEGATIVE_QTY: {li.item} has quantity {li.quantity}")
            check["status"] = "invalid_quantity"
            details["item_checks"].append(check)
            continue

        cur.execute("SELECT stock, unit_price FROM inventory WHERE item = ?", (li.item,))
        row = cur.fetchone()

        if row is None:
            flags.append(f"UNKNOWN_ITEM: {li.item} not found in inventory")
            check["status"] = "unknown"
            check["stock"] = None
        else:
            stock, catalog_price = row
            check["stock"] = stock
            check["catalog_price"] = catalog_price
            if stock == 0:
                flags.append(f"OUT_OF_STOCK / SUSPICIOUS: {li.item} has 0 stock (possible fraud)")
                check["status"] = "zero_stock"
            elif li.quantity > stock:
                flags.append(
                    f"STOCK_MISMATCH: {li.item} requested {li.quantity} but only {stock} available"
                )
                check["status"] = "insufficient_stock"
            else:
                check["status"] = "ok"

            # Price variance vs catalog. Foreign unit prices convert at the dated
            # rate as of the invoice date, and non-USD invoices get an FX buffer so
            # ordinary currency drift isn't mistaken for a pricing problem.
            if li.unit_price is not None and catalog_price:
                fx = fx_rate_asof(extracted.currency, extracted.invoice_date)
                unit_price_usd = round(li.unit_price * fx.rate, 2)
                variance = abs(unit_price_usd - catalog_price) / catalog_price
                is_foreign = fx.currency != "USD"
                tolerance = PRICE_TOLERANCE + (FX_TOLERANCE_BUFFER if is_foreign else 0.0)
                check["price_variance"] = round(variance, 4)
                check["price_tolerance"] = round(tolerance, 4)
                check["fx_rate"] = fx.rate
                check["fx_rate_date"] = fx.as_of
                check["fx_source"] = fx.source
                if variance > tolerance:
                    fx_note = (
                        f" [FX rate {fx.rate:g} as of {fx.as_of} — {fx.source}; "
                        f"FX-adjusted tolerance {tolerance:.0%}]"
                        if is_foreign
                        else ""
                    )
                    flags.append(
                        f"PRICE_VARIANCE: {li.item} billed at {extracted.currency} {li.unit_price:.2f} "
                        f"(${unit_price_usd:.2f} USD) vs catalog ${catalog_price:.2f} "
                        f"({variance:.0%}){fx_note}"
                    )

        details["item_checks"].append(check)

    # Line item arithmetic reconciliation.
    if extracted.line_items and extracted.total is not None:
        computed_subtotal = round(
            sum((li.quantity or 0) * (li.unit_price or 0.0) for li in extracted.line_items), 2
        )
        tax = extracted.tax_amount or 0.0
        shipping = extracted.shipping_amount or 0.0
        expected_total = round(computed_subtotal + tax + shipping, 2)

        if abs(expected_total - extracted.total) > 0.01:
            flags.append(
                f"TOTAL_MISMATCH: Computed items sum (${computed_subtotal:,.2f}) + tax "
                f"(${tax:,.2f}) = ${expected_total:,.2f}, which does not match declared "
                f"total (${extracted.total:,.2f})"
            )

        if extracted.tax_rate is not None and extracted.tax_rate > 0:
            expected_tax = round(computed_subtotal * extracted.tax_rate, 2)
            if abs(expected_tax - tax) > 0.01:
                flags.append(
                    f"TAX_RATE_MISMATCH: Declared tax (${tax:,.2f}) does not match "
                    f"{extracted.tax_rate:.1%} of subtotal (${expected_tax:,.2f})"
                )

    if extracted.total is not None and extracted.total < 0:
        flags.append(f"NEGATIVE_TOTAL: {extracted.total}")

    conn.close()
    return ValidationResult(passed=len(flags) == 0, flags=flags, details=details)
