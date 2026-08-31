#!/usr/bin/env python3
"""Create and seed the mock inventory database (standalone helper)."""
from __future__ import annotations

import argparse
from pathlib import Path

from acme_invoices.config import DEFAULT_DB_PATH
from acme_invoices.persistence import setup_inventory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the mock inventory SQLite database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Database path.")
    parser.add_argument("--keep", action="store_true", help="Keep existing DB (upsert seed rows).")
    args = parser.parse_args(argv)
    setup_inventory(args.db, reset=not args.keep)
    print(f"Inventory database ready at {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
