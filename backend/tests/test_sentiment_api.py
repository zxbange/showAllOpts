from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.models.sentiment import MarketSentiment
from app.core.cache import MODULES, SnapshotCache
from app.core.config import Settings
from app.repositories.sentiment import three_years_ago
from app.services.momentum import china_today


def row(day, above, eligible=100):
    return MarketSentiment(trade_date=day,ratio=Decimal(above)/eligible,above_count=above,
                           eligible_count=eligible,universe_count=eligible+5,updated_at=datetime(2026,10,23,18))


def test_empty_and_strict_thresholds_and_three_year_limit(client,session):
    assert client.get('/api/v1/market-sentiment/history').json()['latest'] is None
    end=china_today()
    values=[19,20,80,81]
    session.add_all([row(end-timedelta(days=3-i),v) for i,v in enumerate(values)])
    session.add_all([row(three_years_ago(end)-timedelta(days=1),50),row(end+timedelta(days=1),99)])
    session.commit()
    data=client.get('/api/v1/market-sentiment/history').json()
    assert [p['state'] for p in data['points']]==['cold','normal','normal','hot']
    assert data['latest']['ratio']==.81
    assert data['summary']['observed_days']==4
    assert data['summary']['cold_days']==data['summary']['hot_days']==1
    assert data['summary']['change_pp']==pytest.approx(62)


def test_display_range_includes_boundary(client,session):
    end=china_today()
    session.add_all([row(end-timedelta(days=30),30),row(end-timedelta(days=29),40),row(end,50)])
    session.commit()
    data=client.get('/api/v1/market-sentiment/history?days=30').json()
    assert data['start_date']==str(end-timedelta(days=29))
    assert data['summary']['observed_days']==2


@pytest.mark.parametrize('query',['days=-1','days=1','days=366','end_date=bad','end_date=2999-01-01'])
def test_invalid_ranges(client,query):
    assert client.get('/api/v1/market-sentiment/history?'+query).status_code==422


def test_incremental_cache_download_and_delete_and_stable_namespace(engine,session,tmp_path,monkeypatch):
    settings=Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,log_dir=tmp_path/'log')
    cache=SnapshotCache(settings,engine)
    with monkeypatch.context() as patch:
        patch.delitem(MODULES,'market-sentiment')
        assert SnapshotCache(settings,engine).root==cache.root
    session.add_all([row(date(2026,10,22),19),row(date(2026,10,23),81)])
    session.commit()
    assert cache.refresh('market-sentiment',background=False)
    with cache.session('market-sentiment') as local:
        assert len(list(local.scalars(select(MarketSentiment))))==2
    # Retention deletion and one new day propagate without forcing full replacement.
    session.delete(session.get(MarketSentiment,date(2026,10,22)))
    session.add(row(date(2026,10,24),20))
    session.commit()
    assert cache.refresh('market-sentiment',background=False)
    with cache.session('market-sentiment') as local:
        assert list(local.scalars(select(MarketSentiment.trade_date).order_by(MarketSentiment.trade_date)))==[date(2026,10,23),date(2026,10,24)]
    assert cache.status()['modules'][list(MODULES).index('market-sentiment')]['ready']
