"""Run as the API service user. No platform requests or existing login profiles."""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app.core.config import Settings
from app.services.social_browser import browser_error


def main():
    settings = Settings()
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.runtime' / 'playwright'))
    print('Python:', sys.version.split()[0])
    print('Service UID:', os.getuid() if hasattr(os, 'getuid') else 'Windows')
    print('Browser directory:', os.environ['PLAYWRIGHT_BROWSERS_PATH'])
    try:
        from playwright.sync_api import sync_playwright
        settings.log_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='browser-check-', dir=settings.log_path) as profile:
            with sync_playwright() as playwright:
                options = {'headless': True, 'chromium_sandbox': os.name != 'nt'}
                if settings.social_browser_channel:
                    options['channel'] = settings.social_browser_channel
                context = playwright.chromium.launch_persistent_context(profile, **options)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_content('<html><body>Browser check</body></html>')
                    page.screenshot(type='png')
                finally:
                    context.close()
        print('PASS: browser launch, profile write and screenshot. Platform network/login not checked.')
        return 0
    except Exception as error:
        code, reason = browser_error(error)
        print('FAIL:', code, reason)
        print('Error type:', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
