#!/usr/bin/env bash
# Update an existing Linux systemd + Nginx installation as its deployment user.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo 'Linux virtual environment missing. Complete scripts/setup.sh and the systemd/Nginx setup first.' >&2
    exit 1
fi
exec "$PYTHON" "$PROJECT_ROOT/scripts/deploy.py" "$@"
