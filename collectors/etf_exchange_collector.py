#!/usr/bin/env python3
"""CLI entry for the packaged exchange ETF collector; dry-run unless --write."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from app.services.etf_exchange import *  # Preserve the documented callable entry points.

if __name__ == '__main__':
    raise SystemExit(main())
