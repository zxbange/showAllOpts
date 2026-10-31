import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from test_short_term_collector import mod, complete_meta


@pytest.fixture
def observation(mod, monkeypatch):
    calendar = [d.date() for d in pd.bdate_range('2026-05-01', periods=120)]
    today = [calendar[100]]
    price_calls, security_calls = [], []

    def trade_days(end_date, count):
        return [d for d in calendar if d <= end_date][-count:]

    def prices(codes, start_date, end_date, **kwargs):
        price_calls.append((list(codes), start_date, end_date, kwargs))
        days = [d for d in calendar if start_date <= d <= end_date]
        close = np.array([10 + calendar.index(d) * .1 for d in days])
        return pd.concat([pd.DataFrame(dict(code=code, time=pd.to_datetime(days),
            open=close-.1, close=close, high=close+.2, low=close-.2, volume=100.)) for code in codes])

    def securities(types, date):
        security_calls.append(date)
        return pd.DataFrame([{'display_name': c, 'start_date': calendar[0]}
                             for c in ('600001.XSHG', '600002.XSHG', '600003.XSHG')],
                            index=['600001.XSHG', '600002.XSHG', '600003.XSHG'])

    apis = dict(get_trade_days=trade_days, get_price=prices, get_all_securities=securities)
    ns = mod._short_term_strategy(apis, mod.DB_CONFIG)
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def database_scope(*args):
        yield Session

    monkeypatch.setattr(mod, 'database_scope', database_scope)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime.combine(today[0], dt.time(17)))

    def save(day, stocks=(), current=False, listed=True):
        meta = complete_meta(ns)
        records = []
        for code, methods in stocks:
            for method in methods:
                meta['counts'][method] += 1
            record = dict(code=code, methods=methods, close=9., pct_change=1., volume=90.,
                          bars=[{'date': day.isoformat(), 'close': 9.}], market_cap=50., pe=12., pb=2.)
            if listed:
                record['listed_date'] = calendar[0].isoformat()
            if current:
                days = trade_days(day, 90)
                record.update(ns.quote_values(ns.clean_history(prices([code], days[0], day), code, days, calendar[0])))
            records.append(record)
        if current:
            meta.update(watch_version=ns.WATCH_VERSION, quotes_as_of=day.isoformat())
        ns.save_day(Session, day, meta, records)

    def contents():
        with Session() as session:
            return ({r.trade_date: r.payload for r in session.query(ns.ShortTermRun)},
                    {(r.trade_date, r.code): r.payload for r in session.query(ns.ShortTermStock)})

    yield SimpleNamespace(ns=ns, mod=mod, Session=Session, engine=engine, calendar=calendar,
                          today=today, prices=prices, price_calls=price_calls, security_calls=security_calls,
                          apis=apis, save=save, contents=contents)
    engine.dispose()


def seed_window(o):
    days = o.calendar[95:101]
    o.save(days[0], [('600001.XSHG', ['bukou', 'redhover']), ('600003.XSHG', ['oversold'])])
    o.save(days[1], [('600002.XSHG', ['xuli'])])
    o.save(days[2], [('600001.XSHG', ['oversold'])])
    o.save(days[3])
    o.save(days[4])
    o.save(days[5], [('600001.XSHG', ['redhover'])], current=True)
    o.price_calls.clear()
    return days


def test_watch_quotes_reuse_today_deduplicate_and_never_rescreen(observation):
    o = observation
    days = seed_window(o)
    before_heads, before = o.contents()
    # No ST/fundamental/signal API supplied: only historical watched codes may be fetched.
    assert o.ns.run().empty
    heads, stocks = o.contents()
    assert len(o.price_calls) == 1
    assert o.price_calls[0][0] == ['600002.XSHG', '600003.XSHG']
    assert o.price_calls[0][3]['fq'] == 'pre' and o.price_calls[0][3]['skip_paused'] is False
    for key, payload in stocks.items():
        assert payload['methods'] == before[key]['methods']
        assert payload['close'] == before[key]['close']  # Signal prices/valuation remain historical.
        assert payload['market_cap'] == 50.
        assert payload['quote_date'] == days[-1].isoformat()
        assert payload['quote_close'] == 20.
        assert len(payload['bars']) == 30 and payload['bars'][-1]['date'] == days[-1].isoformat()
        assert payload['bars'][-1]['ma']['60'] == pytest.approx(np.mean(np.arange(41, 101)*.1+10))
    assert stocks[(days[0], '600001.XSHG')]['bars'] == stocks[(days[-1], '600001.XSHG')]['bars']
    for day in days[:-1]:
        assert heads[day]['snapshot_revision'] != before_heads[day]['snapshot_revision']
        assert heads[day]['counts'] == before_heads[day]['counts']
        assert heads[day]['retention_trade_days'] == 6
    assert heads[days[-1]] == before_heads[days[-1]]
    snapshot = o.contents()
    o.price_calls.clear()
    assert o.ns.run().empty
    assert not o.price_calls and o.contents() == snapshot  # Same-day rerun is idempotent.


def test_expiration_after_fifth_followup_preserves_later_signal(observation):
    o = observation
    days = seed_window(o)
    o.ns.run()
    assert (days[0], '600003.XSHG') in o.contents()[1]  # D+5 is still visible.
    o.today[0] = o.calendar[101]
    o.save(o.today[0], current=True)
    o.price_calls.clear()
    o.ns.run()
    heads, stocks = o.contents()
    assert sorted(heads) == o.calendar[96:102]
    assert all(day != days[0] for day, code in stocks)
    assert all(code != '600003.XSHG' for day, code in stocks)
    assert (days[2], '600001.XSHG') in stocks  # A newer trigger keeps its own lifetime.
    assert all(p['quote_date'] == o.today[0].isoformat() for p in stocks.values())
    assert o.price_calls[0][0] == ['600001.XSHG', '600002.XSHG']


def test_quote_failure_and_database_failure_preserve_all_previous_quotes(observation):
    o = observation
    days = seed_window(o)
    before = o.contents()

    def truncated(*args, **kwargs):
        frame = o.prices(*args, **kwargs)
        return frame[frame['time'].dt.date != days[-1]]

    broken = o.mod._short_term_strategy(dict(o.apis, get_price=truncated), o.mod.DB_CONFIG)
    with pytest.raises(ValueError, match='行情日期不完整'):
        broken.run()
    assert o.contents() == before

    def fail(conn, cursor, sql, *args):
        if sql.startswith('UPDATE short_term_'):
            raise RuntimeError('injected write failure')

    event.listen(o.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            o.ns.run()
    finally:
        event.remove(o.engine, 'before_cursor_execute', fail)
    assert o.contents() == before
    o.ns.run()
    assert o.contents() != before


def test_old_snapshot_migration_only_loads_listing_and_quotes(observation):
    o = observation
    days = o.calendar[95:101]
    for day in days:
        o.save(day, [('600001.XSHG', ['bukou'])], listed=False)
    o.ns.run()
    assert o.security_calls == [days[0]]
    assert len(o.price_calls) == 1 and o.price_calls[0][0] == ['600001.XSHG']
    assert all(p['listed_date'] == o.calendar[0].isoformat() for p in o.contents()[1].values())


@pytest.mark.parametrize('kind', ['short', 'duplicate', 'reverse', 'future'])
def test_invalid_calendar_cannot_delete_existing_signals(observation, kind):
    o = observation
    days = seed_window(o)
    malformed = {'short': days[1:], 'duplicate': days[:-1]+[days[-2]],
                 'reverse': list(reversed(days)), 'future': days[1:]+[o.calendar[101]]}[kind]
    ns = o.mod._short_term_strategy(dict(o.apis, get_trade_days=lambda **kw: malformed), o.mod.DB_CONFIG)
    before = o.contents()
    with pytest.raises(ValueError, match='日历'):
        ns.run()
    assert o.contents() == before and not o.price_calls


def test_upgrade_backfills_only_missing_signal_days(observation):
    o = observation
    days = o.calendar[95:101]
    for day in days[3:]:
        o.save(day, [('600001.XSHG', ['bukou'])])

    class Field:
        def in_(self, value): return self

    fields = SimpleNamespace(**{key: Field() for key in ('code', 'market_cap', 'pe_ratio', 'pb_ratio')})
    ns = o.mod._short_term_strategy(dict(o.apis,
        get_fundamentals=lambda *a, **kw: pd.DataFrame([dict(code=c, market_cap=50, pe_ratio=12, pb_ratio=2)
                      for c in ('600001.XSHG', '600002.XSHG', '600003.XSHG')]),
        get_extras=lambda _, codes, **kw: pd.DataFrame({c: [False] for c in codes}, index=[kw['end_date']]),
        get_industry=lambda *a, **kw: {}, query=lambda *a: SimpleNamespace(filter=lambda *a: None),
        valuation=fields), o.mod.DB_CONFIG)
    ns.run()
    assert o.security_calls == days[:3]
    assert sorted(o.contents()[0]) == days
    assert o.contents()[1][(days[-1], '600001.XSHG')]['methods'] == ['bukou']


def test_concurrent_signal_replacement_cannot_be_overwritten_by_quotes(observation):
    o = observation
    days = seed_window(o)
    before_stocks = o.contents()[1]

    def concurrent(*args, **kwargs):
        raw = o.prices(*args, **kwargs)
        with o.Session.begin() as session:
            head = session.get(o.ns.ShortTermRun, days[0])
            head.payload = dict(head.payload, snapshot_revision='changed-by-another-collector')
        return raw

    ns = o.mod._short_term_strategy(dict(o.apis, get_price=concurrent), o.mod.DB_CONFIG)
    with pytest.raises(RuntimeError, match='快照已变化'):
        ns.run()
    assert o.contents()[1] == before_stocks
    assert o.contents()[0][days[0]]['snapshot_revision'] == 'changed-by-another-collector'


@pytest.mark.parametrize('clock', [dt.datetime(2026, 9, 19, 17), dt.datetime(2026, 9, 21, 15, 59)])
def test_weekend_and_preclose_keep_friday_as_latest(observation, monkeypatch, clock):
    o = observation
    assert o.calendar[100] == dt.date(2026, 9, 18)
    days = seed_window(o)
    monkeypatch.setattr(o.mod, 'china_now', lambda: clock)
    o.ns.run()
    heads, stocks = o.contents()
    assert sorted(heads) == days
    assert all(p['quote_date'] == days[-1].isoformat() for p in stocks.values())
