import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.config import Settings


spec = importlib.util.spec_from_file_location('browser_diagnostic', Path(__file__).resolve().parents[2] / 'scripts/check_social_browser.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def test_screenshot_error_retains_protocol_detail_and_redacts_secrets():
    settings = Settings(_env_file=None, db_password='private-db-value',
                        social_browser_login_key='private-browser-management-key')
    message = ('Page.screenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot\n'
               'password="private-password" token=private-token\n'
               'Cookie: session=private-session\n'
               'https://username:private-url-password@example.com/path?token=private-query\n'
               'private-db-value private-browser-management-key')
    text = diagnostic.safe_error_details(RuntimeError(message), settings)
    assert 'Page.captureScreenshot' in text and 'Unable to capture screenshot' in text
    assert not any(secret in text for secret in ('private-password', 'private-token', 'private-session',
                                                  'private-url-password', 'private-query',
                                                  'private-db-value', 'private-browser-management-key'))


def test_large_error_keeps_beginning_and_final_failure():
    settings = Settings(_env_file=None)
    text = diagnostic.safe_error_details(RuntimeError('Page.screenshot\n' + 'x' * 20000 + '\nFatal last line'), settings)
    assert text.startswith('Page.screenshot') and text.endswith('Fatal last line')
    assert 'truncated' in text and len(text) < 11000


def test_capture_failure_is_reported_before_cleanup(tmp_path, capsys):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    page = MagicMock()
    page.is_closed.return_value = False
    page.screenshot.side_effect = RuntimeError('Page.captureScreenshot: capture failed')
    with patch.object(diagnostic, 'Settings', return_value=settings), patch('playwright.sync_api.sync_playwright') as driver:
        context = driver.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [page]
        assert diagnostic.main() == 1
    output = capsys.readouterr().out
    assert 'Failed stage: capture_screenshot' in output
    assert 'Page.captureScreenshot: capture failed' in output
    assert '"page_closed": false' in output
    context.close.assert_called_once()
