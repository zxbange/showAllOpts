from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import Settings


@pytest.mark.parametrize('failure', [None, 'margin.start', 'validation.stop', 'sources'])
def test_worker_cleanup_covers_startup_and_shutdown_failure(monkeypatch, engine, tmp_path, failure):
    events = []

    def record(name):
        events.append(name)
        if name == failure:
            raise RuntimeError(name)

    def worker(name):
        instance = Mock()
        instance.start.side_effect = lambda: record(name + '.start')
        instance.stop.side_effect = lambda: record(name + '.stop')
        return instance

    for cls, name in [('SnapshotCache', 'cache'), ('SocialMonitor', 'social'),
                      ('BrowserLogin', 'browser'), ('MarginGuarantee', 'margin'),
                      ('ValidationJobs', 'validation')]:
        monkeypatch.setattr(main, cls, Mock(return_value=worker(name)))
    validation_engine = Mock()
    validation_engine.dispose.side_effect = lambda: record('validation_engine.dispose')
    monkeypatch.setattr(main, 'build_engine', Mock(return_value=validation_engine))
    monkeypatch.setattr(main, 'ValidationSources', lambda *_: record('sources'))
    monkeypatch.setattr(main, 'ensure_logging', lambda *_: None)
    monkeypatch.setattr(engine, 'dispose', lambda: record('database.dispose'))
    application = main.create_app(Settings(_env_file=None, log_dir=tmp_path), engine)

    def run():
        with TestClient(application):
            record('serving')

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            run()
    else:
        run()
    assert events[-7:] == ['validation.stop', 'validation_engine.dispose', 'margin.stop',
                           'browser.stop', 'social.stop', 'cache.stop', 'database.dispose']
    assert ('serving' in events) == (failure not in ('sources', 'margin.start'))
