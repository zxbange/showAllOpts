"""All route families share safe, correlated failure and mutation boundaries."""
import logging

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
import pymysql

from app.core.config import Settings
from app.main import create_app


def test_request_boundary_correlates_mutation_without_body_or_query(engine, tmp_path, caplog):
    app = create_app(Settings(_env_file=None, log_dir=tmp_path), engine)
    @app.post('/diagnostics/{item}')
    def write(item: str):
        return {'ok': True}
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        response = client.post('/diagnostics/private-id?arbitrary=private-query',
                               json={'password': 'private-password'}, headers={'X-Request-ID': 'forged'})
    assert response.status_code == 200
    request_id = response.headers['X-Request-ID']
    assert len(request_id) == 12 and request_id != 'forged'
    assert f'request={request_id}' in caplog.text
    assert 'route=/diagnostics/{item}' in caplog.text
    assert 'seconds=' in caplog.text
    assert not any(value in caplog.text for value in ('private-id', 'private-query', 'private-password', 'forged'))


def test_database_boundary_has_driver_code_and_location_not_sql(engine, tmp_path, caplog):
    app = create_app(Settings(_env_file=None, log_dir=tmp_path), engine)
    @app.get('/diagnostics/db')
    def failure():
        raise OperationalError('SELECT secret_table', {'password': 'private-password'},
                               pymysql.err.OperationalError(1045, 'private-driver-message'))
    with caplog.at_level(logging.INFO):
        response = TestClient(app).get('/diagnostics/db')
    assert response.status_code == 503
    assert 'mysql_errno=1045' in caplog.text
    assert 'test_request_diagnostics.py' in caplog.text
    assert not any(value in caplog.text for value in ('secret_table', 'private-password', 'private-driver-message'))


def test_handled_and_unhandled_failures_have_locations(engine, tmp_path, caplog):
    app = create_app(Settings(_env_file=None, log_dir=tmp_path), engine)
    @app.get('/diagnostics/handled')
    def handled():
        raise HTTPException(409, 'private-detail')
    @app.get('/diagnostics/unhandled')
    def unhandled():
        raise RuntimeError('private-runtime-message')
    with caplog.at_level(logging.INFO):
        assert TestClient(app).get('/diagnostics/handled').status_code == 409
        assert TestClient(app, raise_server_exceptions=False).get('/diagnostics/unhandled').status_code == 500
    assert 'Request rejected' in caplog.text and 'status=409' in caplog.text
    assert 'Request failed' in caplog.text and 'test_request_diagnostics.py' in caplog.text
    assert 'private-detail' not in caplog.text and 'private-runtime-message' not in caplog.text


def test_successful_gets_do_not_emit_extra_request_logs(engine, tmp_path, caplog):
    app = create_app(Settings(_env_file=None, log_dir=tmp_path), engine)
    with caplog.at_level(logging.INFO):
        assert TestClient(app).get('/api/v1/health').status_code == 200
    assert 'Request complete' not in caplog.text


def test_polling_template_keeps_prefix_and_slow_success_quiet(engine, tmp_path, caplog, monkeypatch):
    from types import SimpleNamespace
    import app.main as main
    app = create_app(Settings(_env_file=None, log_dir=tmp_path), engine)
    @app.get('/diagnostics/route')
    def template():
        return main.request_route(SimpleNamespace(scope={'route': SimpleNamespace(path='/cache/status')},
                                                   url=SimpleNamespace(path='/api/v1/cache/status')))
    assert TestClient(app).get('/diagnostics/route').json() == '/api/v1/cache/status'
    # Test the actual status route with an auth override and a deterministic slow timer.
    from app.api.routes.accounts import require_user
    app.dependency_overrides[require_user] = lambda: {'id': 'test'}
    values = iter((0.0, 2.0))
    monkeypatch.setattr(main, 'time', SimpleNamespace(monotonic=lambda: next(values)))
    with caplog.at_level(logging.INFO):
        response = TestClient(app).get('/api/v1/cache/status')
    assert response.status_code == 200
    assert 'Request complete' not in caplog.text
