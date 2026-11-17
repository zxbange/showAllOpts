"""Offline A00 incremental recovery, including skipped runs and persisted gaps."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from app.core.config import Settings
from app.services import commodities
from app.services.commodity_source import HOME, CATALOG, QuoteError, parse_recovery, recovery_links


def article_url(index):
    return f'https://m.ccmn.cn/mnewsinfo/{index:032x}.html'


def article(day, *, section='二、现货', published='2026-11-10'):
    ending = '三、预测' if section.startswith('二') else '六、预测'
    return (f'<p>{published}</p><h2>{section}</h2>{int(day[5:7])}月{int(day[8:])}日'
            '长江现货A00铝均价23780元/吨，涨40元/吨，价格区间23760-23800元/吨'
            f'<h2>{ending}</h2>明日长江现货A00铝均价999999元/吨')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 4, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(commodities, 'now', lambda: clock[0])
    seed = tmp_path / 'seed.json'
    seed.write_text('{"rows": []}')
    monkeypatch.setattr(commodities, 'SEED', seed)
    settings = Settings(_env_file=None, log_dir=tmp_path)
    job = commodities.Commodities(settings)
    with job.db() as db:
        row = dict(quoteDate='2026-11-04', low=23000, high=23040, mean=23020,
                   change=20, rangeEvidence='page')
        db.execute('INSERT INTO quotes VALUES(?,?)', (row['quoteDate'], json.dumps(row)))
    clock[0] = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    return clock, settings, job


def install(monkeypatch, available, *, failing=(), home_failure=False, catalogue=True):
    calls = []
    bodies = {article_url(i): article(day) for i, day in enumerate(available, 1)}
    # Use a known valid public homepage layout, replacing only its day/prices.
    from pathlib import Path
    home = (Path(__file__).parent / 'fixtures/commodities/home-visible.html').read_text()
    home = home.replace('11月03日', '2026年11月10日')
    def fetch(client, url, catalog=False):
        calls.append(url)
        if url == HOME:
            if home_failure:
                raise TimeoutError('private-secret')
            return home
        if url == CATALOG:
            return ''.join(f'<a href="{url}">现铝早评</a>' for url in bodies) if catalogue else ''
        if url in failing:
            raise TimeoutError('private-secret')
        return bodies[url]
    monkeypatch.setattr(commodities, 'fetch', fetch)
    return calls, bodies


def days(job):
    return {r['quoteDate'] for r in job.history()['rows']}


@pytest.mark.parametrize('scheduled', [False, True])
def test_manual_and_scheduled_recover_whole_incremental_interval(setup, monkeypatch, scheduled):
    clock, _, job = setup
    original = job.history()['rows'][0]
    calls, _ = install(monkeypatch, ['2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09'])
    assert job.refresh(slot=commodities.latest_slot(clock[0])) if scheduled else job.refresh()
    job.thread.join(5)
    assert not job.thread.is_alive()
    assert days(job) == {'2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09', '2026-11-10'}
    assert job.history()['rows'][0] == original
    state = job.status()
    assert state['currentVerified'] and state['inserted'] == 4 and state['changed'] == 0
    assert state['recovery']['dateFrom'] == '2026-11-04'
    assert state['recovery']['recoveredCount'] == 3
    assert state['recovery']['pendingCount'] == 0  # Weekends are not quote targets.
    assert state['outcome'] == 'completed'
    assert state['recovery']['sourceLimited'] is True
    assert state['trigger'] == ('scheduled' if scheduled else 'manual')
    assert calls[0:2] == [HOME, CATALOG]


def test_restart_preserves_floor_and_retries_source_unavailable_dates(setup, monkeypatch):
    clock, settings, job = setup
    calls, _ = install(monkeypatch, ['2026-11-05', '2026-11-06'], failing=[article_url(2)])
    job.collect(job.lock())
    assert '2026-11-05' in days(job) and '2026-11-06' not in days(job)
    assert 'private-secret' not in str(job.status())
    clock[0] += timedelta(minutes=30)
    restarted = commodities.Commodities(settings)
    # Keep immutable article URLs stable across the restart; add a new article.
    calls, _ = install(monkeypatch, ['2026-11-05', '2026-11-06', '2026-11-09'])
    restarted.collect(restarted.lock())
    assert {'2026-11-05', '2026-11-06', '2026-11-09'} <= days(restarted)
    assert restarted.status()['recovery']['dateFrom'] == '2026-11-04'
    before = restarted.history()['rows']
    restarted.collect(restarted.lock())
    assert restarted.history()['rows'] == before
    assert restarted.status()['inserted'] == restarted.status()['changed'] == 0


def test_request_budget_rotates_and_remembers_public_links(setup, monkeypatch):
    clock, settings, job = setup
    job.max_requests = 3  # Homepage, catalogue, one historical article.
    install(monkeypatch, ['2026-11-05', '2026-11-06', '2026-11-09'], failing=[article_url(1)])
    job.collect(job.lock())
    state = job.status()
    assert state['recovery']['requestCount'] == 3 and state['recovery']['limited']
    assert state['recovery']['deferredArticles'] == 2
    clock[0] += timedelta(minutes=30)
    restarted = commodities.Commodities(settings)
    restarted.max_requests = 3
    calls, _ = install(monkeypatch, ['2026-11-05', '2026-11-06', '2026-11-09'], catalogue=False)
    restarted.collect(restarted.lock())
    assert calls == [HOME, CATALOG, article_url(2)]
    assert '2026-11-06' in days(restarted)


def test_empty_install_starts_today_without_historical_backfill(setup, monkeypatch):
    _, _, job = setup
    with job.db() as db:
        db.execute('DELETE FROM quotes')
        db.execute('UPDATE schedule_meta SET initialized_at=?', (commodities.now().isoformat(),))
    calls, _ = install(monkeypatch, ['2026-11-05'])
    job.collect(job.lock())
    assert calls == [HOME]
    assert days(job) == {'2026-11-10'}
    assert job.status()['recovery']['dateFrom'] == '2026-11-10'


def test_history_can_recover_when_homepage_fails(setup, monkeypatch):
    _, _, job = setup
    install(monkeypatch, ['2026-11-05', '2026-11-06'], home_failure=True)
    job.collect(job.lock())
    assert days(job) == {'2026-11-04', '2026-11-05', '2026-11-06'}
    assert not job.status()['currentVerified']
    assert job.status()['outcome'] == 'partial'


@pytest.mark.parametrize('section', ['二、现货', '五、长江市场行情'])
def test_recovery_parser_verifies_dated_spot_sections(section):
    row = parse_recovery(article('2026-11-06', section=section), article_url(1), today=date(2026, 11, 10))
    assert (row['quoteDate'], row['low'], row['mean'], row['high'], row['change']) == ('2026-11-06', 23760, 23780, 23800, 40)


@pytest.mark.parametrize('old,new', [('11月6日', ''), ('长江现货', '广东现货'),
                                    ('23760-23800', '23800-23760'), ('涨40', '涨'),
                                    ('2026-11-10', '2026-11-11'), ('二、现货', '六、预测')])
@pytest.mark.parametrize('section', ['二、现货', '五、长江市场行情'])
def test_invalid_recovery_evidence_rejected(old, new, section):
    with pytest.raises(QuoteError):
        parse_recovery(article('2026-11-06', section=section).replace(section if old=='二、现货' else old, new), article_url(1), today=date(2026, 11, 10))


def test_discovery_keeps_only_safe_relevant_interval_links():
    body = (f'<a href="{article_url(1)}">2026-11-06现铝早评</a>'
            f'<a href="{article_url(2)}">2026-10-30现铝早评</a>'
            f'<a href="{article_url(3)}">铜价行情</a>'
            '<a href="https://evil.example/file">现铝早评</a>')
    assert recovery_links(body, start='2026-11-04', end='2026-11-10') == [article_url(1)]


def test_parsed_articles_cool_down_but_requests_and_new_links_retry(setup, monkeypatch):
    clock, settings, job = setup
    calls, _ = install(monkeypatch, ['2026-11-05', '2026-11-06'], failing=[article_url(2)])
    job.collect(job.lock())
    restarted = commodities.Commodities(settings)
    calls, _ = install(monkeypatch, ['2026-11-05', '2026-11-06', '2026-11-09'])
    restarted.collect(restarted.lock())
    assert article_url(1) not in calls
    assert article_url(2) in calls and article_url(3) in calls
    assert {'2026-11-05', '2026-11-06', '2026-11-09'} <= days(restarted)
    calls.clear()
    restarted.collect(restarted.lock())
    assert calls == [HOME]
    clock[0] += timedelta(days=1)
    calls.clear()
    restarted.collect(restarted.lock())
    assert article_url(1) not in calls  # No historical gap remains.


def test_current_quote_is_logged_and_saved_before_recovery(setup, monkeypatch, caplog):
    _, _, job = setup
    calls, bodies = install(monkeypatch, ['2026-11-05'])
    original = commodities.fetch
    def fetch(client, url, catalog=False):
        if url == CATALOG:
            assert job.status()['currentVerified']
            assert '2026-11-10' in days(job)
        return original(client, url, catalog)
    monkeypatch.setattr(commodities, 'fetch', fetch)
    with caplog.at_level('INFO'):
        job.collect(job.lock())
    assert 'Commodity current quote target=2026-11-10 source_date=2026-11-10 result=current' in caplog.text


def test_unrelated_article_has_specific_diagnostic():
    from app.services.commodity_source import ArticleNotApplicable, diagnostic_reason
    assert diagnostic_reason(ArticleNotApplicable('unrelated')) == 'article_not_applicable'


def test_recovery_log_identifies_unverified_dates(setup, monkeypatch, caplog):
    _, _, job = setup
    install(monkeypatch, ['2026-11-05', '2026-11-06', '2026-11-09'])
    with caplog.at_level('INFO'):
        job.collect(job.lock())
    assert 'pending=0 limited=False pending_dates=-' in caplog.text
    assert not {'2026-11-07', '2026-11-08'} & days(job)

@pytest.mark.parametrize('instant', [
    datetime(2026,11,7,3,tzinfo=timezone.utc),
    datetime(2026,11,8,3,tzinfo=timezone.utc),
    datetime(2026,10,1,3,tzinfo=timezone.utc),
    datetime(2027,1,4,3,tzinfo=timezone.utc)])
def test_closed_automatic_skips_but_manual_failure_is_isolated(setup, monkeypatch, instant):
    clock, _, job = setup
    clock[0] = instant
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('unavailable')
    monkeypatch.setattr(commodities, 'fetch', fail)
    before = job.history()['rows']
    slot = commodities.latest_slot(clock[0])
    assert not job.refresh(slot=slot)
    assert not calls
    job.collect(job.lock(), slot)
    assert not calls
    assert job.refresh()
    job.thread.join(5)
    assert calls
    assert job.history()['rows'] == before
    with job.db() as db:
        for table in ('failed_targets','recovery_attempts','a00_source_checks','a00_recovery'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0

def test_restart_retires_weekend_targets_not_quotes(setup):
    _, settings, job = setup
    with job.db() as db:
        for day in ('2026-11-07','2026-11-08','2026-11-09'):
            db.execute('INSERT INTO failed_targets VALUES(?,?,?)', (day,'failed','old'))
            db.execute('INSERT INTO recovery_attempts VALUES(?,?,?)', (day,'old','failed'))
        db.execute('INSERT INTO quotes VALUES(?,?)', ('2026-11-07', json.dumps({'quoteDate':'2026-11-07','mean':123})))
    before = job.history()['rows']
    restarted = commodities.Commodities(settings)
    assert restarted.history()['rows'] == before
    with restarted.db() as db:
        for table in ('failed_targets','recovery_attempts'):
            assert [r[0] for r in db.execute(f'SELECT day FROM {table}')] == ['2026-11-09']

def test_manual_closed_failure_does_not_stop_next_normal_schedule(setup, monkeypatch):
    clock, _, job = setup
    clock[0] = datetime(2026,11,7,3,tzinfo=timezone.utc)
    monkeypatch.setattr(commodities, 'fetch', lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    job.collect(job.lock())
    clock[0] = datetime(2026,11,10,10,tzinfo=timezone.utc)
    install(monkeypatch, ['2026-11-05','2026-11-06','2026-11-09'])
    assert job.refresh(slot=commodities.latest_slot(clock[0]))
    job.thread.join(5)
    assert job.status()['outcome'] == 'completed'
    assert not {'2026-11-07','2026-11-08'} & days(job)

def test_automatic_collection_stops_before_second_request_after_china_midnight(setup, monkeypatch):
    clock, _, job = setup
    clock[0] = datetime(2026,11,6,15,59,tzinfo=timezone.utc)
    calls = []
    from pathlib import Path
    home = (Path(__file__).parent / 'fixtures/commodities/home-visible.html').read_text()
    def fetch(client, url, catalog=False):
        calls.append(url)
        clock[0] = datetime(2026,11,6,16,0,tzinfo=timezone.utc)
        return home
    monkeypatch.setattr(commodities, 'fetch', fetch)
    job.collect(job.lock(), commodities.latest_slot(clock[0]))
    assert calls == [HOME]
    assert job.status()['outcome'] == 'interrupted'
    assert not job.status()['currentVerified']
    lock = job.lock()
    assert lock is not None
    lock.release()

def test_known_holiday_targets_retired_but_unknown_calendar_preserved(setup):
    _, settings, job = setup
    with job.db() as db:
        for day in ('2026-10-01', '2027-01-04'):
            db.execute('INSERT INTO failed_targets VALUES(?,?,?)', (day,'failed','old'))
    restarted = commodities.Commodities(settings)
    assert [x['day'] for x in restarted.status()['pendingTargets']] == ['2027-01-04']


def test_manual_failure_crossing_into_weekend_keeps_prior_queue(setup, monkeypatch):
    clock, _, job = setup
    clock[0] = datetime(2026,11,6,15,59,tzinfo=timezone.utc)
    with job.db() as db:
        db.execute('INSERT INTO failed_targets VALUES(?,?,?)', ('2026-11-05','old_failure','old'))
        db.execute('INSERT INTO a00_source_checks VALUES(?,?,?)', (article_url(1),'old','pending'))
        before = {table: db.execute(f'SELECT * FROM {table}').fetchall()
                  for table in ('failed_targets','recovery_attempts','a00_source_checks')}
    def fail(*args, **kwargs):
        clock[0] = datetime(2026,11,6,16,0,tzinfo=timezone.utc)
        raise TimeoutError('unavailable')
    monkeypatch.setattr(commodities, 'fetch', fail)
    job.collect(job.lock())
    with job.db() as db:
        for table, rows in before.items():
            assert db.execute(f'SELECT * FROM {table}').fetchall() == rows
    assert job.status()['outcome'] == 'failed'
