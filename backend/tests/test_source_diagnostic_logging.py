"""Failures are diagnosed before safe wrappers remove the original exception."""
from contextlib import nullcontext
import logging
from types import SimpleNamespace

import httpx
import pymysql
import pytest

from app.services import database_sources, social_notifications


def test_database_source_logs_original_stage_code_without_config(monkeypatch, caplog):
    source = object.__new__(database_sources.DatabaseSources)
    monkeypatch.setattr(source, 'allowed', lambda config: '127.0.0.1')
    monkeypatch.setattr(database_sources, 'create_engine', lambda *args, **kwargs: kwargs['creator'])
    def fail(**kwargs):
        raise pymysql.err.OperationalError(1045, 'private-driver-message')
    monkeypatch.setattr(database_sources.pymysql, 'connect', fail)
    config = dict(host='private-host', port=3306, username='private-user', password='private-password', database='private-db')
    with caplog.at_level(logging.WARNING), pytest.raises(database_sources.SourceError):
        source.build_engine(config)()
    assert 'stage=create_driver' in caplog.text and 'mysql_errno=1045' in caplog.text
    assert not any(secret in caplog.text for secret in (*config.values(), 'private-driver-message') if isinstance(secret, str))


def test_notification_logs_status_without_webhook_or_payload(monkeypatch, caplog):
    webhook = 'https://open.feishu.cn/open-apis/bot/v2/hook/private-webhook-id-123456789'
    response = httpx.Response(403, json={'code': 19021, 'msg': 'private-body'})
    monkeypatch.setattr(social_notifications.httpx, 'Client', lambda **kwargs: nullcontext(SimpleNamespace(post=lambda *args, **kwargs: response)))
    with caplog.at_level(logging.WARNING), pytest.raises(social_notifications.NotificationError):
        social_notifications.send_feishu(webhook, 'private-secret', 'private-content')
    assert 'http_status=403' in caplog.text and 'provider_code=19021' in caplog.text
    assert not any(value in caplog.text for value in (webhook, 'private-secret', 'private-content', 'private-body'))


def test_etf_transport_error_is_logged_before_wrapper_removes_cause(caplog):
    from app.services.etf_exchange import ExchangeClient, SourceError, SZSE_CALENDAR
    def get(*args, **kwargs):
        raise httpx.ReadTimeout('private-url-token')
    client = ExchangeClient(SimpleNamespace(get=get), attempts=2, sleep=lambda _: None)
    with caplog.at_level(logging.WARNING), pytest.raises(SourceError):
        client.get(SZSE_CALENDAR, {}, 'https://www.szse.cn/')
    assert caplog.text.count('ETF source request failed') == 2
    assert 'source=szse_calendar' in caplog.text and 'reason=http_timeout' in caplog.text
    assert 'private-url-token' not in caplog.text


def test_index_fallback_logs_each_source_without_raw_errors(monkeypatch, caplog):
    from datetime import date
    from app.services import growth_dividend_source as source
    def fail_tencent(*args):
        raise httpx.ReadTimeout('private-tencent-token')
    def fail_eastmoney(*args):
        raise httpx.ReadTimeout('private-eastmoney-token')
    monkeypatch.setattr(source, 'fetch_tencent', fail_tencent)
    monkeypatch.setattr(source, 'fetch_eastmoney', fail_eastmoney)
    monkeypatch.setattr(source.time, 'sleep', lambda _: None)
    day = date(2026, 11, 10)
    with caplog.at_level(logging.WARNING), pytest.raises(source.IndexSourceError):
        source.fetch_prices(None, SimpleNamespace(code='000001'), day, day, expected_dates={day})
    assert caplog.text.count('Index source request failed') == 3
    assert 'source=tencent' in caplog.text and 'source=eastmoney' in caplog.text
    assert 'reason=http_timeout' in caplog.text
    assert 'private-tencent-token' not in caplog.text and 'private-eastmoney-token' not in caplog.text
