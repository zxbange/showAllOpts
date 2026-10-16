#!/usr/bin/env bash
# Run as the deployment user, without sudo. Never changes system packages.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON_BIN:-python3}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python)
            [[ $# -ge 2 ]] || { echo '--python requires an executable path.' >&2; exit 2; }
            PYTHON="$2"
            shift 2
            ;;
        -h|--help)
            echo 'Usage: bash scripts/setup.sh [--python python3.11]'
            echo 'Installs dependencies, initializes config/app.config or validates APP_ENV_FILE, and builds the frontend.'
            exit 0
            ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
command -v "$PYTHON" >/dev/null || { echo "Python not found: $PYTHON" >&2; exit 1; }
command -v node >/dev/null || { echo 'Install Node.js 22.12+ and npm first.' >&2; exit 1; }
command -v npm >/dev/null || { echo 'npm was not found on PATH.' >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required.")'
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 12)) { console.error("Node.js 22.12+ is required."); process.exit(1); }'
VENV="$PROJECT_ROOT/backend/.venv"
if [[ -e "$VENV" && ! -x "$VENV/bin/python" ]]; then
    echo 'backend/.venv is not a usable Linux environment. Do not copy a Windows venv.' >&2
    echo 'Move that directory outside this project, then rerun setup.' >&2
    exit 1
fi
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Existing venv requires Python 3.11+; recreate it with the requested interpreter.")'
"$VENV/bin/python" -m pip install -r "$PROJECT_ROOT/backend/requirements.lock.txt"
"$VENV/bin/python" "$PROJECT_ROOT/scripts/configure_database.py" --production
cd "$PROJECT_ROOT/frontend"
npm ci --no-fund --no-audit
npm run build
echo 'Setup complete. Check the selected configuration (APP_ENV_FILE or config/app.config), then run: bash start.sh'
echo 'For a persistent production service, see docs/linux-deployment.md.'
