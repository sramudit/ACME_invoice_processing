#!/usr/bin/env python3
"""Thin CLI shim so the pipeline runs as `python main.py --invoice_path=...`."""
from __future__ import annotations

from acme_invoices.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
