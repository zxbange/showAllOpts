"""Offline source fixtures for the real unified cache-to-page history path."""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.engine import make_url
from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.services import growth_dividend_import as growth, valuation_ruler_import as valuation
from app.services.valuation_ruler_model import validate_day


def seed_history_sources(engine, day=date(2020, 1, 2)):
    growth.metadata.create_all(engine)
    valuation.metadata.create_all(engine)
    stamp = datetime.combine(day, datetime.min.time())
    members = {f'{i:06d}.XSHE': Decimal('10') for i in range(300)}
    daily = validate_day(day, '4000', members, valuation.SOURCE)
    daily.update(trade_date=day, updated_at=stamp)
    with engine.begin() as conn:
        conn.execute(growth.CALENDAR.insert(), dict(trade_date=day, source=growth.CALENDAR_SOURCE, updated_at=stamp))
        conn.execute(growth.PRICES.insert(), [dict(index_code=code, trade_date=day, close=1000.0 + i,
            source=growth.SOURCE, updated_at=stamp) for i, code in enumerate(growth.CODES)])
        conn.execute(valuation.CALENDAR.insert(), dict(trade_date=day, source=valuation.CALENDAR_SOURCE, updated_at=stamp))
        conn.execute(valuation.DAILY.insert(), daily)
        conn.execute(valuation.MEMBER.insert(), [dict(trade_date=day,index_code=valuation.INDEX,
            method_version=valuation.METHOD,code=code,pe_ttm=value,source=valuation.SOURCE,updated_at=stamp)
            for code,value in members.items()])


def run_history_sync(service, engine, *, module='valuation-ruler', moment=None, full=False):
    if not hasattr(engine, 'url'):
        engine.url = make_url('mysql+pymysql://reader@fixture/history')
    settings = Settings(_env_file=None, cache_enabled=True, cache_auto_refresh=False,
                        log_dir=service.root.parents[1])
    cache = SnapshotCache(settings, engine)
    cache.history_services[module] = service
    if moment is None:
        cache.refresh(module, background=False, full=full)
    else:
        with patch('app.core.cache.now', return_value=moment):
            cache.refresh(module, background=False, full=full)
    return cache
