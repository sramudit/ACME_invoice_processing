"""Ingestion: one deterministic/heuristic parser per invoice format.

Structured inputs (JSON / CSV / XML) are parsed deterministically; unstructured
text (TXT / PDF) is handed to an offline heuristic extractor (``MockLLM``) that
mimics an LLM's structured output, so the whole pipeline runs with no network or
API key. PDF text is read with PyMuPDF (falling back to pdfplumber).
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import SUPPORTED_EXTS
from .models import ExtractedInvoice, LineItem


# ---- currency & number / OCR helpers ---------------------------------------
def detect_currency(text: str) -> str:
    """Extract currency ISO code or symbol; defaults to USD if unspecified."""
    if not text:
        return "USD"
    m = re.search(r"\b(EUR|USD|GBP|CAD|AUD|JPY|CHF)\b", str(text), re.I)
    if m:
        return m.group(1).upper()
    if "$" in str(text):
        return "USD"
    if "€" in str(text):
        return "EUR"
    if "£" in str(text):
        return "GBP"
    if "¥" in str(text):
        return "JPY"
    return "USD"


_OCR_DIGIT_FIXES = {"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"}


def clean_number(raw) -> Optional[float]:
    """Parse a money/quantity token, tolerating $, commas and OCR digit slips."""
    if raw is None:
        return None
    token = str(raw).strip().replace("$", "").replace(",", "").strip()
    if not token:
        return None
    if re.search(r"[OolISB]", token) and re.fullmatch(r"[\dOolISB.\-]+", token):
        token = "".join(_OCR_DIGIT_FIXES.get(ch, ch) for ch in token)
    try:
        return float(token)
    except ValueError:
        return None


def normalize_date(raw_date: Optional[str]) -> Optional[str]:
    """Convert raw invoice date strings into uniform YYYY-MM-DD ISO format."""
    if (
        not raw_date
        or not isinstance(raw_date, str)
        or raw_date.strip().lower() in ("nan", "none", "")
    ):
        return None

    val = raw_date.strip()
    # Fix OCR typos where letter 'O' was scanned instead of '0'.
    val = val.replace("2O26", "2026").replace("2O25", "2025")

    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d %Y",
        "%d-%b-%Y", "%b-%d-%Y", "%d %b %Y", "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return val  # Retains unparseable strings (e.g. "yesterday") for validation flags


def strip_parenthetical(name: str) -> tuple[str, Optional[str]]:
    """Split 'WidgetA (rush order)' into ('WidgetA', 'rush order')."""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", name.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return name.strip(), None


def _f(v) -> Optional[float]:
    return clean_number(v) if v not in (None, "") else None


# ---- offline heuristic extractor for unstructured TXT/PDF ------------------
_SKIP_PREFIXES = (
    "subtotal", "sub total", "tax", "total", "grand total", "shipping", "amt",
    "amount", "item", "description", "notes", "note", "terms", "payment",
    "from", "to:", "to ", "inv", "invoice", "date", "due", "bill", "vendor",
    "vndr", "ref", "----", "====", "attn", "please", "hi", "best",
    "thank", "accounts", "subject",
)


class MockLLM:
    """Deterministic, offline stand-in that mimics an LLM's structured output."""

    name = "mock-heuristic"

    def extract_invoice(self, text: str) -> dict:
        lines = text.splitlines()
        joined = "\n".join(lines)
        return {
            "invoice_number": self._invoice_number(joined),
            "vendor_name": self._vendor(lines),
            "date": self._field(lines, ("date", "dt")),
            "due_date": self._due(lines),
            "line_items": self._line_items(lines),
            "total": self._total(lines),
            "currency": self._currency(joined),
            "notes": self._notes(joined),
            "tax_rate": self._tax_rate(joined),
            "tax_amount": self._tax(joined),
        }

    def _invoice_number(self, text: str) -> str:
        m = re.search(
            r"(?:invoice\s*number|inv\s*#|inv\s*no|invoice|inv)\s*[:#]?\s*"
            r"(INV[\s-]?\d+|\d{3,})",
            text,
            re.I,
        )
        if not m:
            return ""
        digits = re.search(r"\d+", m.group(1))
        return f"INV-{digits.group(0)}" if digits else ""

    def _vendor(self, lines: list[str]) -> str:
        for ln in lines:
            m = re.match(r"\s*(?:vendor|vndr)\s*[:]\s*(.+)$", ln, re.I)
            if m:
                return self._trim_trailing_labels(m.group(1))
        for ln in lines:
            m = re.match(r"\s*from\s*[:]\s*(.+)$", ln, re.I)
            if m and "@" not in m.group(1):
                return self._trim_trailing_labels(m.group(1))
        return ""

    @staticmethod
    def _trim_trailing_labels(value: str) -> str:
        return re.split(
            r"\s+(?:due|date|terms|inv\b|invoice)\b", value, maxsplit=1, flags=re.I
        )[0].strip()

    def _field(self, lines: list[str], keys: tuple[str, ...]) -> Optional[str]:
        full_text = "\n".join(lines)
        date_token = (
            r"(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+[0-9OolISB]{4}|"
            r"[0-9OolISB]{1,2}[-/\s][A-Za-z0-9OolISB]{3,9}[-/\s,]+[0-9OolISB]{2,4}|"
            r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"
        )
        for k in keys:
            pattern = rf"\b{k}\b[\s:|]*?({date_token})"
            m = re.search(pattern, full_text, re.I)
            if m and not re.search(r"\bdue\b", m.group(0), re.I):
                return m.group(1).strip()
        return None

    def _due(self, lines: list[str]) -> Optional[str]:
        full_text = "\n".join(lines)
        date_token = (
            r"(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+[0-9OolISB]{4}|"
            r"[0-9OolISB]{1,2}[-/\s][A-Za-z0-9OolISB]{3,9}[-/\s,]+[0-9OolISB]{2,4}|"
            r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"
        )
        pattern = rf"\b(?:due\s*date|due\s*dt|due)\b[\s:|]*?({date_token})"
        m = re.search(pattern, full_text, re.I)
        return m.group(1).strip() if m else None

    def _tax(self, text: str) -> float:
        m = re.search(r"\btax(?:\s*\([^)]*\))?\s*[:|]?[\s|]*\$?\s*([\d.,OolISB]+)", text, re.I)
        return clean_number(m.group(1)) if m else 0.0

    def _tax_rate(self, text: str) -> Optional[float]:
        """Extract tax percentage (e.g. 'Tax (5%):' -> 0.05)."""
        m = re.search(r"\btax(?:\s*rate)?\s*\(\s*([\d.]+)\%\s*\)", text, re.I)
        if not m:
            m = re.search(r"\btax\s*rate\s*[:|]?\s*([\d.]+)\%", text, re.I)
        return float(m.group(1)) / 100.0 if m else 0.0

    def _shipping(self, text: str) -> float:
        m = re.search(r"\b(?:shipping|freight|handling)\b[\s:|]*?\$?\s*([\d.,OolISB]+)", text, re.I)
        return clean_number(m.group(1)) if m else 0.0

    def _currency(self, text: str) -> str:
        return detect_currency(text)

    def _total(self, lines: list[str]) -> Optional[float]:
        for ln in lines:
            if re.match(r"\s*(?:total amount|total|grand total|amt)\s*[:]", ln, re.I):
                m = re.search(r"\$?\s*([\d.,OolISB]+)", re.split(r"[:]", ln, maxsplit=1)[-1])
                if m:
                    val = clean_number(m.group(1))
                    if val is not None:
                        return val
        return None

    def _notes(self, text: str) -> Optional[str]:
        m = re.search(r"notes?\s*[:]\s*(.+)", text, re.I)
        return m.group(1).strip() if m else None

    def _line_items(self, lines: list[str]) -> list[dict]:
        items: list[dict] = []
        for raw in lines:
            s = raw.strip()
            if not s or s.lower().startswith(_SKIP_PREFIXES):
                continue
            item = self._parse_item(s)
            if item:
                items.append(item)
        return items

    def _parse_item(self, s: str) -> Optional[dict]:
        # 1) Labeled: "WidgetA qty: 10 unit price: $250.00"
        m = re.match(
            r"^(.+?)\s+qty[:]?\s*(\d+)\s*(?:@|unit\s*price[:]?|\$)?\s*\$?([\d.,OolISB]+)?",
            s,
            re.I,
        )
        if not m:  # 2) Email bullet: "- SuperGizmo x12 $400.00 each"
            m = re.match(r"^[-*]\s*(.+?)\s+x\s*(\d+)\s+\$?([\d.,OolISB]+)", s, re.I)
        if not m:  # 3) Table row: "WidgetA 8 $250.00 $2,000.00"
            m = re.match(r"^([A-Za-z][A-Za-z .()\-]*?)\s+(\d+)\s+\$?([\d.,OolISB]+)", s)
        if not m:
            return None

        name, note = strip_parenthetical(m.group(1).strip())
        qty = clean_number(m.group(2))
        price = clean_number(m.group(3)) if m.lastindex and m.group(3) else 0.0
        if not name or qty is None:
            return None
        item: dict = {"item": name, "quantity": qty, "unit_price": price or 0.0}
        if note:
            item["note"] = note
        return item


_OFFLINE_LLM = MockLLM()  # deterministic extraction for TXT/PDF (no network)


# ---- shared builders -------------------------------------------------------
def _effective_total(
    items: list[LineItem],
    declared: Optional[float],
    tax_amount: Optional[float] = 0.0,
    shipping_amount: Optional[float] = 0.0,
) -> Optional[float]:
    """Best-available total: declared total, else sum of line items + tax."""
    if declared is not None:
        return declared
    subtotal = sum((li.quantity or 0) * (li.unit_price or 0.0) for li in items)
    return round(subtotal + (tax_amount or 0.0), 2)


def _build_extracted(
    path: Path,
    *,
    invoice_number: str,
    vendor: str,
    invoice_date: Optional[str] = None,
    due_date: Optional[str],
    items: list[LineItem],
    declared_total: Optional[float],
    subtotal: Optional[float] = None,
    tax_rate: Optional[float] = 0.0,
    tax_amount: Optional[float] = 0.0,
    shipping_amount: Optional[float] = 0.0,
    currency: str,
    notes: Optional[str],
    revision: Optional[str],
    warnings: list[str],
) -> ExtractedInvoice:
    anomalies: list[str] = []
    if revision:
        anomalies.append(f"Revision noted: {revision}")
    if notes:
        anomalies.append(f"Notes: {notes}")
    anomalies.extend(warnings or [])

    calc_subtotal = (
        subtotal
        if subtotal is not None
        else round(sum((li.quantity or 0) * (li.unit_price or 0.0) for li in items), 2)
    )

    return ExtractedInvoice(
        invoice_number=invoice_number or path.stem,
        vendor=vendor,
        invoice_date=normalize_date(invoice_date),
        due_date=normalize_date(due_date),
        line_items=items,
        subtotal=calc_subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount or 0.0,
        shipping_amount=shipping_amount or 0.0,
        total=_effective_total(items, declared_total, tax_amount, shipping_amount),
        currency=currency or "USD",
        raw_notes=notes or "",
        anomalies=anomalies,
    )


# ---- JSON ------------------------------------------------------------------
def ingest_json(path: Path) -> ExtractedInvoice:
    with open(path) as f:
        data = json.load(f)

    vendor_data = data.get("vendor", {})
    if isinstance(vendor_data, dict):
        vendor_name = str(vendor_data.get("name") or "").strip()
    else:
        vendor_name = str(vendor_data or "").strip()

    items = []
    for li in data.get("line_items", []):
        items.append(
            LineItem(
                item=str(li.get("item", "UNKNOWN")),
                quantity=int(li.get("quantity", 0)),
                unit_price=float(li["unit_price"]) if "unit_price" in li else None,
            )
        )

    anomalies = []
    if not vendor_name:
        vendor_name = "UNKNOWN"
        anomalies.append("MISSING_VENDOR: Vendor field is blank")
    if data.get("revision"):
        anomalies.append(f"Revision noted: {data['revision']}")
    if data.get("notes"):
        anomalies.append(f"Notes: {data['notes']}")

    raw_rate = data.get("tax_rate")
    tax_rate = float(raw_rate) if raw_rate is not None else 0.0

    raw_curr = data.get("currency")
    currency = raw_curr.strip().upper() if raw_curr else detect_currency(json.dumps(data))

    return ExtractedInvoice(
        invoice_number=data.get("invoice_number", path.stem),
        vendor=vendor_name,
        invoice_date=normalize_date(data.get("date") or data.get("invoice_date")),
        due_date=normalize_date(data.get("due_date")),
        line_items=items,
        tax_rate=tax_rate,
        subtotal=float(data["subtotal"]) if "subtotal" in data else None,
        tax_amount=float(data["tax_amount"]) if "tax_amount" in data else 0.0,
        total=float(data["total"]) if "total" in data else None,
        currency=currency,
        raw_notes=data.get("notes", ""),
        anomalies=anomalies,
    )


# ---- CSV (key/value layout and tabular layout) ----------------------------
def ingest_csv(path: Path) -> ExtractedInvoice:
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header = [c.strip().lower() for c in rows[0]] if rows else []
    if header and header[0] == "field":  # key/value layout
        return _from_kv_csv(rows, path)
    return _from_tabular_csv(rows, header, path)


def _kv_item(d: dict) -> LineItem:
    return LineItem(
        item=d.get("item", ""),
        quantity=int(_f(d.get("quantity")) or 0),
        unit_price=_f(d.get("unit_price")) or 0.0,
    )


def _from_kv_csv(rows: list[list[str]], path: Path) -> ExtractedInvoice:
    meta: dict = {}
    items: list[LineItem] = []
    pending: dict = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key, val = row[0].strip().lower(), row[1].strip()
        if key == "item":
            if pending:
                items.append(_kv_item(pending))
            pending = {"item": val}
        elif key in ("quantity", "qty"):
            pending["quantity"] = val
        elif key in ("unit_price", "price"):
            pending["unit_price"] = val
        else:
            meta[key] = val
    if pending:
        items.append(_kv_item(pending))

    rate_str = meta.get("tax_rate")
    tax_rate = float(rate_str) if rate_str else 0.0
    currency = str(meta.get("currency") or "").strip().upper() or detect_currency(str(rows))

    return _build_extracted(
        path,
        invoice_number=str(meta.get("invoice_number") or ""),
        vendor=str(meta.get("vendor") or ""),
        invoice_date=meta.get("date") or meta.get("invoice_date"),
        due_date=meta.get("due_date"),
        items=items,
        tax_rate=tax_rate,
        declared_total=_f(meta.get("total")),
        currency=currency,
        notes=meta.get("notes"),
        revision=meta.get("revision"),
        warnings=[],
    )


def _from_tabular_csv(rows: list[list[str]], header: list[str], path: Path) -> ExtractedInvoice:
    def col(*names):
        lower_header = [h.lower() for h in header]
        for n in names:
            if n.lower() in lower_header:
                return lower_header.index(n.lower())
        return None

    ci_num = col("invoice number", "invoice_number")
    ci_vendor = col("vendor")
    ci_date = col("date")
    ci_due = col("due date", "due_date")
    ci_item = col("item")
    ci_qty = col("qty", "quantity")
    ci_price = col("unit price", "unit_price")

    inv_num = vendor = date = due = ""
    items: list[LineItem] = []
    total = None
    tax = 0.0

    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        item_val = row[ci_item].strip() if ci_item is not None and ci_item < len(row) else ""
        label = " ".join(c.strip().lower() for c in row)

        if "tax:" in label and not item_val:
            m = [c for c in row if c.strip()]
            tax = _f(m[-1]) if m else 0.0
            continue
        if "total:" in label and not item_val:
            m = [c for c in row if c.strip()]
            total = _f(m[-1]) if m else total
            continue
        if not item_val:
            continue
        if ci_num is not None and not inv_num:
            inv_num = row[ci_num].strip()
        if ci_vendor is not None and not vendor:
            vendor = row[ci_vendor].strip()
        if ci_date is not None and not date:
            date = row[ci_date].strip()
        if ci_due is not None and not due:
            due = row[ci_due].strip()
        items.append(
            LineItem(
                item=item_val,
                quantity=int(_f(row[ci_qty]) or 0) if ci_qty is not None else 0,
                unit_price=_f(row[ci_price]) or 0.0 if ci_price is not None else 0.0,
            )
        )

    return _build_extracted(
        path,
        invoice_number=inv_num,
        vendor=vendor,
        invoice_date=date or None,
        due_date=due or None,
        items=items,
        tax_amount=tax,
        declared_total=total,
        currency=detect_currency(" ".join(" ".join(r) for r in rows)),
        notes=None,
        revision=None,
        warnings=[],
    )


# ---- XML ------------------------------------------------------------------
def ingest_xml(path: Path) -> ExtractedInvoice:
    root = ET.parse(path).getroot()

    def text(tag: str) -> Optional[str]:
        el = root.find(f".//{tag}")
        return el.text.strip() if el is not None and el.text else None

    items = []
    for it in root.findall(".//line_items/item"):
        items.append(
            LineItem(
                item=(it.findtext("name") or "").strip(),
                quantity=int(_f(it.findtext("quantity")) or 0),
                unit_price=_f(it.findtext("unit_price")) or 0.0,
            )
        )

    rate_str = text("tax_rate")
    tax_rate = float(rate_str) if rate_str else 0.0
    currency = text("currency") or detect_currency(ET.tostring(root, encoding="unicode"))

    return _build_extracted(
        path,
        invoice_number=text("invoice_number") or "",
        vendor=text("vendor") or "",
        invoice_date=text("date") or text("invoice_date"),
        due_date=text("due_date"),
        items=items,
        tax_rate=tax_rate,
        tax_amount=_f(text("tax_amount")) or 0.0,
        declared_total=_f(text("total")),
        currency=currency,
        notes=None,
        revision=None,
        warnings=[],
    )


# ---- unstructured TXT / PDF (via MockLLM) ---------------------------------
def read_text(path: Path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _words_to_lines(page, y_tol: float = 3.0) -> str:
    """Group a page's words into visual rows (same baseline) ordered left→right."""
    words = page.get_text("words")
    if not words:
        return page.get_text()
    rows: list[list] = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if rows and abs(w[1] - rows[-1][0][1]) <= y_tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    lines = []
    for row in rows:
        ordered = sorted(row, key=lambda w: w[0])
        lines.append(" ".join(w[4] for w in ordered))
    return "\n".join(lines)


def pdf_text(path: Path) -> str:
    try:  # PyMuPDF preferred — rejoin table cells that render as separate lines
        import pymupdf

        with pymupdf.open(path) as doc:
            return "\n".join(_words_to_lines(page) for page in doc)
    except Exception:
        pass
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass
    twin = path.with_suffix(".txt")  # last resort: a sibling .txt twin
    if twin.exists():
        return read_text(twin)
    raise RuntimeError("PDF extraction requires PyMuPDF or pdfplumber (pip install PyMuPDF).")


def read_source_text(path: Path) -> str:
    """Raw text of a source document (PDF via extractor, else file contents)."""
    return pdf_text(path) if path.suffix.lower() == ".pdf" else read_text(path)


def ingest_unstructured(path: Path) -> ExtractedInvoice:
    text = read_source_text(path)
    data = _OFFLINE_LLM.extract_invoice(text)
    tax_rate = _OFFLINE_LLM._tax_rate(text)
    tax_amount = _OFFLINE_LLM._tax(text)
    shipping_amount = _OFFLINE_LLM._shipping(text)

    currency = data.get("currency") or detect_currency(text)
    warnings: list[str] = []
    items: list[LineItem] = []

    for li in data.get("line_items", []):
        try:
            items.append(
                LineItem(
                    item=li.get("item", ""),
                    quantity=int(li.get("quantity", 0) or 0),
                    unit_price=li.get("unit_price", 0.0) or 0.0,
                    note=li.get("note"),
                )
            )
        except Exception:
            warnings.append(f"Dropped unparseable line item: {li!r}")
    if not items:
        warnings.append("No line items could be extracted from unstructured input.")

    return _build_extracted(
        path,
        invoice_number=data.get("invoice_number", ""),
        vendor=data.get("vendor_name", "") or "",
        invoice_date=data.get("date"),
        due_date=data.get("due_date"),
        items=items,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        shipping_amount=shipping_amount,
        declared_total=data.get("total"),
        currency=currency,
        notes=data.get("notes"),
        revision=None,
        warnings=warnings,
    )


# ---- item-name canonicalization -------------------------------------------
def catalog_item_index(db_path: Path) -> dict[str, str]:
    """Map a normalized key (no spaces, lowercased) -> canonical catalog item name."""
    try:
        conn = sqlite3.connect(db_path)
        names = [r[0] for r in conn.execute("SELECT item FROM inventory")]
        conn.close()
    except Exception:
        names = []
    return {re.sub(r"\s+", "", n).lower(): n for n in names}


def canonicalize_item(name: str, index: dict) -> str:
    """'Widget A' -> 'WidgetA' when it matches a catalog item; else unchanged."""
    if not name:
        return name
    return index.get(re.sub(r"\s+", "", name).lower(), name.strip())


# ---- dispatcher ------------------------------------------------------------
_PARSERS = {
    ".json": ingest_json,
    ".csv": ingest_csv,
    ".xml": ingest_xml,
    ".txt": ingest_unstructured,
    ".pdf": ingest_unstructured,
}


def ingest_invoice(path: Path, db_path: Optional[Path] = None) -> ExtractedInvoice:
    """Per-format parse, then normalize item names to the catalog spelling."""
    suffix = path.suffix.lower()
    parser = _PARSERS.get(suffix)
    if parser is None:
        raise ValueError(f"Unsupported format: {suffix}")
    inv = parser(path)
    if db_path is not None:
        index = catalog_item_index(db_path)
        for li in inv.line_items:
            li.item = canonicalize_item(li.item, index)
    return inv


def invoice_files(folder: Path) -> list[Path]:
    """All supported invoice files in a folder, sorted for stable ordering."""
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTS)
