"""Run as the API service user. No platform requests or existing login profiles."""
import os
import json
import re
import shutil
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote, quote_plus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app.core.config import Settings
from app.core.logging import ANSI_ESCAPE, CREDENTIAL_URL, SECRET_FIELD
from app.services.social_browser import browser_error, WIDTH, HEIGHT


def safe_error_details(error, settings):
    # This script only opens an empty temporary profile and inline HTML, never an account.
    text = ANSI_ESCAPE.sub('', str(error))
    text = CREDENTIAL_URL.sub(r'\1[REDACTED]@', text)
    text = SECRET_FIELD.sub(r'\1[REDACTED]', text)
    text = re.sub(r'(?im)^.*\b(?:cookie|set-cookie)\s*[:=].*$', '[REDACTED COOKIE HEADER]', text)
    text = re.sub(r'(https?://[^\s?#]+)\?[^\s]*', r'\1?[REDACTED QUERY]', text)
    for value in (settings.db_password.get_secret_value(), settings.social_browser_login_key.get_secret_value()):
        if value:
            for variant in (value, quote(value, safe=''), quote_plus(value)):
                text = text.replace(variant, '[REDACTED]')
    if len(text) > 10000:
        text = text[:3000] + '\n[diagnostic middle truncated]\n' + text[-7000:]
    return text


def main():
    settings = Settings()
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.runtime' / 'playwright'))
    print('Python:', sys.version.split()[0])
    print('Service UID:', os.getuid() if hasattr(os, 'getuid') else 'Windows')
    print('Browser directory:', os.environ['PLAYWRIGHT_BROWSERS_PATH'])
    print('Browser mode:', 'headless' if settings.social_browser_headless else 'window')
    channel = settings.social_browser_channel
    print('Browser channel:', channel if channel in ('', 'chrome', 'chromium', 'msedge') else 'custom')
    display = os.environ.get('DISPLAY', '')
    authority = os.environ.get('XAUTHORITY', '')
    runtime = {
        'display_set': bool(display),
        'display': display if re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}', display) else '',
        'xauthority_set': bool(authority),
        'xauthority_readable': bool(authority and os.access(authority, os.R_OK)),
        'xvfb_installed': shutil.which('Xvfb') is not None,
        'xauth_installed': shutil.which('xauth') is not None,
    }
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            name, _, value = line.partition(':')
            if name in ('NoNewPrivs', 'Seccomp'):
                runtime[name] = int(value.strip())
    except (OSError, ValueError):
        pass
    print('Display environment:', json.dumps(runtime, sort_keys=True))
    stage = 'load_playwright'
    page = None
    events = {'page_crashed': False, 'browser_disconnected': False}
    try:
        from playwright.sync_api import sync_playwright
        stage = 'prepare_profile'
        settings.log_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='browser-check-', dir=settings.log_path) as profile:
            stage = 'start_playwright'
            with sync_playwright() as playwright:
                options = {'headless': settings.social_browser_headless, 'chromium_sandbox': os.name != 'nt',
                           'viewport': {'width': WIDTH, 'height': HEIGHT},
                           'accept_downloads': False, 'service_workers': 'block'}
                if settings.social_browser_channel:
                    options['channel'] = settings.social_browser_channel
                stage = 'launch_browser'
                context = playwright.chromium.launch_persistent_context(profile, **options)
                if context.browser:
                    context.browser.on('disconnected', lambda _: events.update(browser_disconnected=True))
                try:
                    stage = 'render_test_page'
                    page = context.pages[0] if context.pages else context.new_page()
                    page.on('crash', lambda _: events.update(page_crashed=True))
                    page.set_content('<html><body>Browser check</body></html>')
                    stage = 'capture_screenshot'
                    page.screenshot(type='png', timeout=15000)
                except BaseException:
                    # Record state before our own cleanup closes the context.
                    print('Page state at failure:', json.dumps({
                        'page_closed': page.is_closed() if page else None, **events,
                    }, sort_keys=True))
                    try:
                        context.close()
                    except Exception:
                        pass
                    raise
                else:
                    stage = 'close_browser'
                    context.close()
        print('PASS: browser launch, profile write and screenshot. Platform login and systemd environment not checked.')
        return 0
    except Exception as error:
        code, reason = browser_error(error)
        print('FAIL:', code, reason)
        print('Failed stage:', stage)
        print('Error type:', type(error).__name__)
        print('Error details (redacted):\n' + safe_error_details(error, settings))
        print('For Chromium startup logs, run this temporary-profile check with DEBUG=pw:browser.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
