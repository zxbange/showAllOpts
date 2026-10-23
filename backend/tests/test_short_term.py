from datetime import date, datetime
from sqlalchemy import event
from app.models.short_term import ShortTermRun, ShortTermStock
from app.core.cache import SnapshotCache, MODULES
from app.core.config import Settings
from test_cache import cached


def seed(session):
    for day in (date(2026, 10, 21), date(2026, 10, 22)):
        session.add(ShortTermRun(trade_date=day, payload={'counts': {'bukou': 7, 'redhover': 0, 'xuli': 1}}, updated_at=datetime(2026,10,22)))
        for i in range(7):
            session.add(ShortTermStock(trade_date=day, code=str(i), payload={'name': str(i), 'methods': ['bukou'] + (['xuli'] if i == 0 else []), 'bars': []}, updated_at=datetime(2026,10,22)))
    session.commit()


def test_method_dates_pagination_and_empty(client, session):
    path = '/api/v1/short-term/latest'
    assert client.get(path).json()['trade_date'] is None
    seed(session)
    data = client.get(path).json()
    assert data['trade_date'] == '2026-10-22' and data['total'] == 7 and len(data['items']) == 6
    assert len(client.get(path+'?offset=6').json()['items']) == 1
    assert client.get(path+'?method=redhover').json()['items'] == []
    assert client.get(path+'?method=xuli&day=2026-10-21').json()['total'] == 1
    assert client.get(path+'?method=wrong').status_code == 422
    assert client.get(path+'?limit=10000').status_code == 422
    assert client.get(path+'?day=2026-10-20').status_code == 404


def test_cache_prunes_expired_source_rows_and_namespace_unchanged(cached, session, engine, monkeypatch):
    _, cache = cached
    with monkeypatch.context() as patch:
        patch.delitem(MODULES, 'short-term')
        old = SnapshotCache(cache.settings, engine)
        assert old.root == cache.root
    seed(session)
    cache.refresh('short-term', background=False)
    assert not cache.read_state()['modules']['short-term']['error']
    session.query(ShortTermStock).filter(ShortTermStock.trade_date == date(2026,10,21)).delete()
    session.query(ShortTermRun).filter(ShortTermRun.trade_date == date(2026,10,21)).delete()
    session.commit()
    cache.refresh('short-term', background=False)
    def fail(*args):
        raise AssertionError('local page queried source')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        with cache.session('short-term') as local:
            assert local.query(ShortTermRun).count() == 1
            assert local.query(ShortTermStock).count() == 7
    finally:
        event.remove(engine, 'before_cursor_execute', fail)
