import io
import logging
import multiprocessing
import re
import time
from pathlib import Path

from app.core.config import Settings
from app.core.logging import StandardFormatter, configure_logging, daily_handler


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
