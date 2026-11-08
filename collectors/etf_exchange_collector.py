#!/usr/bin/env python3
"""CLI entry for the packaged exchange ETF collector; dry-run unless --write."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from app.services.etf_exchange import *  # Preserve the documented callable entry points.
from app.services.etf_exchange import main as _exchange_main
from mysql_connector import create_mysql_engine


def main(argv=None):
    return _exchange_main(argv, engine_factory=create_mysql_engine)

if __name__ == '__main__':
    raise SystemExit(main())
