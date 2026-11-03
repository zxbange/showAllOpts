from datetime import date, datetime, timezone
from pathlib import Path
import pytest
from app.services import commodities
from app.services.commodity_source import (HOME, CATALOG, QuoteError, parse_home,
    home_date_reference, public_report_date)
from app.core.config import Settings

FIXTURE = Path(__file__).parent/'fixtures'/'commodities'
HOME_HTML = (FIXTURE/'home-visible.html').read_text()
DATE_HTML = (FIXTURE/'public-date-visible.html').read_text()
DATE_URL = 'https://m.ccmn.cn/mnewsinfo/9614f9cf8c9a46b595891b1b7501f3cb.html'


def test_public_home_quote_uses_market_anchor_and_explicit_date():
    assert home_date_reference(HOME_HTML) == DATE_URL
    day = public_report_date(DATE_HTML)
    row = parse_home(HOME_HTML, today=date(2026,11,4), report_date=day, report_url=DATE_URL)
    assert (row['quoteDate'], row['low'], row['high'], row['mean'], row['change']) == ('2026-11-03',23430,23470,23450,-70)
    assert not {'open','close','volume'} & row.keys()
    assert row['sourceUrl']==HOME


@pytest.mark.parametrize('old,new', [('cjxh/', 'gd/'), ('11月03日','11月04日'),
    ('23,430—23,470','24,430—24,470'), ('A00铝</span>','A00铝美元/吨</span>')])
def test_ambiguous_or_wrong_market_never_accepted(old,new):
    with pytest.raises(QuoteError):
        parse_home(HOME_HTML.replace(old,new),today=date(2026,11,4),report_date=date(2026,11,3),report_url=DATE_URL)


def test_missing_date_reference_and_future_date_rejected():
    with pytest.raises(QuoteError):home_date_reference(HOME_HTML.replace('11月03日长江有色金属现货行情','其他文章'))
    with pytest.raises(QuoteError):public_report_date('<h1>登录后查看</h1>')
    with pytest.raises(QuoteError):public_report_date(DATE_HTML.replace('2026-11-03','2026-11-02'))
    with pytest.raises(QuoteError):parse_home(HOME_HTML,today=date(2026,11,2),report_date=date(2026,11,3),report_url=DATE_URL)


def service(tmp_path, monkeypatch):
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,3,3,tzinfo=timezone.utc))
    job=commodities.Commodities(Settings(_env_file=None,log_dir=tmp_path))
    seed=tmp_path/'seed.json';seed.write_text('{"rows": []}')
    monkeypatch.setattr(commodities,'SEED',seed)
    monkeypatch.setattr(job.stopping,'wait',lambda *_:False)
    monkeypatch.setattr(commodities,'discover',lambda *_:[])
    def fetch(client,url,catalog=False):
        return {HOME:HOME_HTML,DATE_URL:DATE_HTML,CATALOG:'<html></html>'}[url]
    monkeypatch.setattr(commodities,'fetch',fetch)
    return job


def test_current_collection_counts_and_idempotence(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch)
    job.collect(job.lock());s=job.status()
    assert (s['inserted'],s['changed'],s['pagesFetched'],s['pagesParsed'])==(1,0,3,1)
    assert s['latestQuoteDate']=='2026-11-03' and s['outcome']=='completed' and not s['stale']
    job.collect(job.lock());s=job.status()
    assert s['inserted']==s['changed']==0 and s['noNew'] and s['stage']=='暂无新报价'
    assert len(job.history()['rows'])==19


def test_directory_failure_preserves_verified_current_quote(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch);original=commodities.fetch
    def fetch(client,url,catalog=False):
        if url==CATALOG:raise TimeoutError('private')
        return original(client,url,catalog)
    monkeypatch.setattr(commodities,'fetch',fetch)
    job.collect(job.lock())
    assert job.status()['outcome']=='partial' and job.status()['currentVerified']
    assert job.history()['rows'][-1]['quoteDate']=='2026-11-03'


def test_home_failure_cannot_claim_current_success_or_delete_history(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch);before=job.history()['rows'];original=commodities.fetch
    def fetch(client,url,catalog=False):
        if url==HOME:raise TimeoutError('private')
        return original(client,url,catalog)
    monkeypatch.setattr(commodities,'fetch',fetch)
    job.collect(job.lock());s=job.status()
    assert s['outcome']=='failed' and not s['currentVerified'] and s['stale']
    assert s['latestQuoteDate']=='2026-11-02' and s['checkedAt']
    assert 'private' not in str(s) and job.history()['rows']==before


def test_no_relabel_on_later_collection_day(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch)
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,4,3,tzinfo=timezone.utc))
    job.collect(job.lock());s=job.status()
    assert s['stale'] and s['latestQuoteDate']==s['sourceQuoteDate']=='2026-11-03'


def test_old_home_at_11_is_not_today_success(tmp_path, monkeypatch):
    job=service(tmp_path,monkeypatch)
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,4,3,tzinfo=timezone.utc))
    job.collect(job.lock(),datetime(2026,11,4,3,tzinfo=timezone.utc))
    s=job.status()
    assert s['targetDate']=='2026-11-04' and s['targetResult']=='sourceNotPublished'
    assert s['outcome']=='no_quote' and not s['currentVerified']


def test_18_updates_same_quote_and_requires_complete_fields(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch)
    job.collect(job.lock())
    original=commodities.fetch
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,3,10,tzinfo=timezone.utc))
    def revised(client,url,catalog=False):
        body=original(client,url,catalog)
        return body.replace('23,430—23,470','23,440—23,480').replace('23,450','23,460') if url==HOME else body
    monkeypatch.setattr(commodities,'fetch',revised)
    slot=datetime(2026,11,3,10,tzinfo=timezone.utc)
    job.collect(job.lock(),slot)
    s=job.status()
    assert s['targetMode']=='complete_spot' and s['currentVerified']
    assert s['changed']==1 and s['inserted']==0 and len(job.history()['rows'])==19
    before=job.history()['rows']
    def incomplete(client,url,catalog=False):
        return revised(client,url,catalog).replace('23,440—23,480','--')
    monkeypatch.setattr(commodities,'fetch',incomplete)
    job.collect(job.lock(),slot)
    assert job.status()['targetResult']=='partial' and not job.status()['currentVerified']
    assert job.history()['rows']==before


def test_delayed_slot_keeps_original_target_day(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch)
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,4,0,tzinfo=timezone.utc))
    job.collect(job.lock(),datetime(2026,11,3,10,tzinfo=timezone.utc))
    s=job.status()
    assert s['targetDate']=='2026-11-03' and s['currentVerified'] and s['stale']
