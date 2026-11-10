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
    assert state['recovery']['pendingCount'] == 2  # Weekends stay unknown, never fabricated.
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
    calls, _ = install(monkeypatch, ['2026-11-06', '2026-11-09'])
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
