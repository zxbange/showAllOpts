from datetime import date, datetime
from decimal import Decimal
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from app.models.dividend import DividendSnapshot, DividendStock
from app.repositories.dividend import DividendRepository
from test_dividend_collector import row, meta


def seed(session,empty=False):
    data=meta()
    if empty:data.update(value_count=0,value_candidates=0)
    session.add(DividendSnapshot(id=1,updated_at=datetime(2026,10,16,17),**data))
    if not empty:session.add_all([DividendStock(**row()),DividendStock(**row('bank', code='600000.XSHG'))])
    session.commit()


def test_empty_uncollected_and_collected_zero_are_distinct(client,session):
    data=client.get('/api/v1/dividend-selection/latest').json()
    assert data['snapshot'] is None and len(data['groups'])==2
    seed(session,empty=True)
    data=client.get('/api/v1/dividend-selection/latest').json()
    assert data['snapshot']['trade_date']=='2026-10-16' and all(not g['items'] for g in data['groups'])


def test_latest_order_units_overlap_and_single_read_only_query(client,session,engine):
    seed(session)
    session.add(DividendStock(**row(code='000002.XSHE',rank=2)));session.commit()
    statements=[]
    def capture(conn,cursor,sql,*args):statements.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:response=client.get('/api/v1/dividend-selection/latest')
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert response.status_code==200
    data=response.json()
    assert [r['rank'] for r in data['groups'][0]['items']]==[1,2]
    assert data['groups'][0]['items'][0]['dividend_yield_static']==5
    assert 'overlap_codes' not in data
    assert 'dividend_ratio_3y' not in data['groups'][0]['items'][0]
    assert len([sql for sql in statements if sql.lstrip().startswith('SELECT')]) == 1
    assert all(sql.lstrip().startswith(('SELECT', 'PRAGMA')) for sql in statements)
    assert data['groups'][0]['items'][0]['dividend_yield_history'] == [
        dict(fiscal_year=year, yield_pct=value) for year, value in [(2025,5),(2024,4),(2023,3)]]
    assert client.post('/api/v1/dividend-selection/latest').status_code==405


def test_error_is_sanitized_and_module_registered(client,monkeypatch):
    def fail(*args):raise OperationalError('SELECT',{},Exception('password=secret'))
    monkeypatch.setattr(DividendRepository,'latest',fail)
    response=client.get('/api/v1/dividend-selection/latest')
    assert response.status_code==503 and 'secret' not in response.text
    assert 'dividend-selection' in [m['id'] for m in client.get('/api/v1/modules').json()['modules']]


def test_retired_method_is_never_exposed(client,session):
    seed(session)
    session.add(DividendStock(**row('classic', code='600001.XSHG')))
    session.commit()
    data=client.get('/api/v1/dividend-selection/latest').json()
    assert [g['id'] for g in data['groups']] == ['value', 'bank']
    assert data['groups'][1]['items'][0]['code'] == '600000.XSHG'
    assert all(r['method'] != 'classic' for g in data['groups'] for r in g['items'])
    assert 'classic_count' not in data['snapshot'] and 'overlap_codes' not in data
    assert data['requires_refresh'] is False


@pytest.mark.parametrize('version', ['dividend_top25_v2', 'dividend_split_bank_v3', 'dividend_static_fy_v4', 'dividend_static_3pct_v5'])
def test_legacy_yields_are_not_mislabeled_as_static(client,session,version):
    seed(session)
    session.get(DividendSnapshot, 1).rule_version=version
    session.commit()
    data=client.get('/api/v1/dividend-selection/latest').json()
    assert data['requires_refresh'] is True
    assert not data['groups'][0]['items']
    assert not data['groups'][1]['items']
    assert 'overlap_codes' not in data
