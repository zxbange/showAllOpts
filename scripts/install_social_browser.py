"""Install Chromium into a deployment-local directory, outside protected home dirs.

Run with backend/.venv/bin/python as the deployment owner after installing requirements.
Linux system libraries: sudo backend/.venv/bin/python -m playwright install-deps chromium
"""
import os
from pathlib import Path
import subprocess
import sys


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(root / '.runtime' / 'playwright'))
    subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], env=env, check=True)
