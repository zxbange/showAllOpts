import io
import logging
import multiprocessing
import re
import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.logging import PollingAccessFilter, StandardFormatter, configure_logging, daily_handler, logging_config


def access_record(path, status=200, method='GET', level=logging.INFO, name='uvicorn.access'):
    return logging.LogRecord(name, level, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ('127.0.0.1:1234', method, path, '1.1', status), None)


@pytest.mark.parametrize('path', sorted(PollingAccessFilter.paths))
@pytest.mark.parametrize('status', [200, 204, 299, 304])
def test_polling_access_filter_omits_only_successful_gets(path, status):
    assert not PollingAccessFilter().filter(access_record(path, status))
    assert not PollingAccessFilter().filter(access_record(path + '?poll=1', status))


@pytest.mark.parametrize('status', [100, 199, 300, 301, 302, 307, 400, 401, 403, 404, 409, 422, 429, 500, 503])
def test_polling_access_filter_preserves_failures_and_redirects(status):
    for path in PollingAccessFilter.paths:
        assert PollingAccessFilter().filter(access_record(path, status))


def test_polling_access_filter_preserves_other_methods_paths_levels_and_formats():
    filter_ = PollingAccessFilter()
    for path in PollingAccessFilter.paths:
        for method in ('POST', 'HEAD', 'DELETE'):
            assert filter_.filter(access_record(path, method=method))
        for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
            assert filter_.filter(access_record(path, level=level))
        assert filter_.filter(access_record(path, name='app.test'))
        assert filter_.filter(access_record(path, status='200'))
        assert filter_.filter(access_record(path + '/extra'))
    for path in ('/api/v1/health', '/api/v1/etf-shares/collection-test', '/api/v1/modules'):
        assert filter_.filter(access_record(path))
    malformed = access_record('/api/v1/cache/status')
    malformed.args = ()
    assert filter_.filter(malformed)
    malformed.args = {'status': 200}
    assert filter_.filter(malformed)


def test_access_filter_applies_to_console_and_app_log_without_hiding_errors(tmp_path, capsys):
    configure_logging(Settings(_env_file=None, log_dir=tmp_path))
    try:
        logger = logging.getLogger('uvicorn.access')
        logger.handle(access_record('/api/v1/cache/status'))
        logger.handle(access_record('/api/v1/etf-shares/collection-status', 304))
        logger.handle(access_record('/api/v1/cache/status', 503))
        logger.handle(access_record('/api/v1/etf-shares/collection-test', 409, 'POST'))
        logging.getLogger('app.test').error('important-database-error')
        console = capsys.readouterr().err
        application = (tmp_path / 'app.log').read_text(encoding='utf-8')
        for output in (console, application):
            assert 'HTTP/1.1" 200' not in output and 'HTTP/1.1" 304' not in output
            assert 'HTTP/1.1" 503' in output and 'HTTP/1.1" 409' in output
            assert 'important-database-error' in output
        assert 'important-database-error' in (tmp_path / 'error.log').read_text(encoding='utf-8')
    finally:
        logging.shutdown()


def write_process_logs(folder, worker):
    settings = Settings(_env_file=None, log_dir=Path(folder))
    configure_logging(settings)
    for index in range(12):
        logging.getLogger('app.worker').info('worker-%s-record-%s', worker, index)
    logging.shutdown()


def test_files_format_error_split_and_secret_redaction(tmp_path):
    password = 'sensitive-pass-123'
    configure_logging(Settings(_env_file=None, log_dir=tmp_path, db_password=password))
    try:
        assert (tmp_path / 'app.log').exists()
        assert (tmp_path / 'error.log').exists()
        assert (tmp_path / 'error.log').stat().st_size == 0
        logger = logging.getLogger('app.test')
        logger.info('normal-event')
        try:
            raise RuntimeError('password=unknown-secret token=hidden-token ' + password)
        except RuntimeError:
            logger.exception('failed-event')
        app_text = (tmp_path / 'app.log').read_text(encoding='utf-8')
        error_text = (tmp_path / 'error.log').read_text(encoding='utf-8')
        assert 'normal-event' in app_text and 'normal-event' not in error_text
        assert 'failed-event' in app_text and 'failed-event' in error_text
        assert 'Traceback' in error_text
        assert all(value not in app_text for value in (password, 'unknown-secret', 'hidden-token'))
        assert '[REDACTED]' in error_text
        assert all(re.match(r'^\d{4}-\d{2}-\d{2}T.*[+-]\d\d:\d\d \| (INFO|ERROR)\s+\| pid=\d+ \| ', line)
                   for line in app_text.splitlines())
    finally:
        logging.shutdown()


def test_formatter_strips_color_and_redacts_urls_for_console_too():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StandardFormatter(secrets=['a b@c']))
    record = logging.LogRecord('frontend', logging.ERROR, __file__, 1,
                               '\x1b[31mfailed\x1b[0m mysql://user:pass@db/app a%20b%40c\nsecond-line', (), None)
    handler.handle(record)
    text = stream.getvalue()
    assert '\x1b' not in text and 'user:pass' not in text and 'a%20b%40c' not in text
    assert text.count('| frontend |') == 2


def test_collection_writer_passwords_and_encoded_variants_are_redacted(tmp_path):
    from urllib.parse import quote, quote_plus
    password = 'writer-only pass@2026'
    settings = Settings(_env_file=None, log_dir=tmp_path,
                        market_collection_writer_url='mysql+pymysql://writer:' + quote(password, safe='') + '@db.example/market',
                        etf_collection_writer_url='mysql+pymysql://writer:etf-private-pass@db.example/market')
    formatter = StandardFormatter(logging_config(settings)['formatters']['standard']['secrets'])
    record = logging.LogRecord('app.test', logging.ERROR, __file__, 1,
                               'driver: ' + password + ' ' + quote(password, safe='') + ' ' + quote_plus(password) + ' etf-private-pass', (), None)
    result = formatter.format(record)
    assert all(value not in result for value in (password, quote(password, safe=''), quote_plus(password), 'etf-private-pass'))


def test_daily_rollover_keeps_existing_data_and_limits_archives(tmp_path):
    handler = daily_handler(tmp_path / 'app.log', retention_days=2)
    handler.setFormatter(StandardFormatter())
    logger = logging.getLogger('rotation-test')
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info('before-midnight')
        # Advance the shared rotation deadline without sleeping or changing the system clock.
        handler.clh._do_lock()
        try:
            handler.rolloverAt = int(time.time()) - 1
            handler.write_rollover_time()
        finally:
            handler.clh._do_unlock()
        (tmp_path / 'app.log.2000-01-01').write_text('oldest', encoding='utf-8')
        (tmp_path / 'app.log.2000-01-02').write_text('old', encoding='utf-8')
        logger.info('after-midnight')
        archives = list(tmp_path.glob('app.log.*'))
        assert len(archives) == 2
        assert not (tmp_path / 'app.log.2000-01-01').exists()
        assert any('before-midnight' in file.read_text(encoding='utf-8') for file in archives)
        assert 'after-midnight' in (tmp_path / 'app.log').read_text(encoding='utf-8')
        assert handler.when == 'MIDNIGHT' and handler.interval == 86400
    finally:
        handler.close()
        logger.handlers.clear()


def test_two_processes_write_without_missing_or_duplicate_records(tmp_path):
    context = multiprocessing.get_context('spawn')
    workers = [context.Process(target=write_process_logs, args=(str(tmp_path), worker)) for worker in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
        text = (tmp_path / 'app.log').read_text(encoding='utf-8')
        lines = text.splitlines()
        assert len(lines) == 24
        for worker in range(2):
            for index in range(12):
                assert sum(line.endswith(f'worker-{worker}-record-{index}') for line in lines) == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)


def test_formatter_redacts_unsaved_headers_webhooks_and_request_queries():
    formatter = StandardFormatter()
    message = ('Authorization: Bearer bearer-value\nCookie: sid=cookie-value; other=other-value\n'
               'url=https://example.test/webhook/private-path?custom=private-query\n'
               'api_key=private-api-key refresh_token=private-refresh\n'
               'safe-count=12')
    record = logging.LogRecord('app.test', logging.ERROR, __file__, 1, message, (), None)
    output = formatter.format(record)
    for secret in ('bearer-value', 'cookie-value', 'other-value', 'private-path',
                   'private-query', 'private-api-key', 'private-refresh'):
        assert secret not in output
    assert 'safe-count=12' in output
    access = access_record('/api/v1/unknown?arbitrary=private-query', 500)
    assert 'private-query' not in formatter.format(access)
    assert 'HTTP/1.1" 500' in formatter.format(access)
    assert access.args[2].endswith('private-query')


def test_formatter_never_outputs_raw_exception_or_source_lines():
    formatter = StandardFormatter()
    try:
        raise ValueError('unlabelled-sensitive-value')
    except ValueError:
        import sys
        record = logging.LogRecord('uvicorn.error', logging.ERROR, __file__, 1,
                                   'Exception in ASGI application', (), sys.exc_info())
    output = formatter.format(record)
    assert 'unlabelled-sensitive-value' not in output
    assert 'error_type=ValueError' in output and 'test_logging.py' in output
    assert 'Traceback diagnostics:' in output
