#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo 'Linux Python environment not found. Run bash scripts/setup.sh first.' >&2
    exit 1
fi
exec "$PYTHON" "$PROJECT_ROOT/scripts/dev.py" "$@"
