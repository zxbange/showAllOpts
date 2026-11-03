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
    def fetch(client,url,catalog=False):
        return {HOME:HOME_HTML,DATE_URL:DATE_HTML,CATALOG:'<html></html>'}[url]
    monkeypatch.setattr(commodities,'fetch',fetch)
    return job


def test_current_collection_counts_and_idempotence(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch)
    job.collect(job.lock());s=job.status()
    assert (s['inserted'],s['changed'],s['pagesFetched'],s['pagesParsed'])==(1,0,2,1)
    assert s['latestQuoteDate']=='2026-11-03' and s['outcome']=='completed' and not s['stale']
    job.collect(job.lock());s=job.status()
    assert s['inserted']==s['changed']==0 and s['noNew'] and s['stage']=='暂无新报价'
    assert len(job.history()['rows'])==19




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


def test_discovery_filters_other_metals_and_classifies_unrelated_articles():
    from app.services.commodity_source import discover, parse, ArticleNotApplicable, diagnostic_reason
    article='https://m.ccmn.cn/mnewsinfo/b9076f1604594cdfa5653a30cf4fdec8.html'
    other='https://m.ccmn.cn/mnewsinfo/e6171de13c7741479f62f160cabacc96.html'
    assert discover(f'<a href="{article}">乌镇AI机器人点燃铝轻量化</a><a href="{other}">铜市场新闻</a>')==[article]
    body='<h1>乌镇AI机器人点燃铝轻量化</h1><p>2026-11-02 产业需求观察</p>'
    with pytest.raises(ArticleNotApplicable):parse(body,article)
    # Known historical sources and relevant-but-changed layouts remain errors.
    for content, known in [(body, {'quoteDate':'2026-10-30'}),
                           ('<h1>现铝报价观察</h1><p>长江现货A00铝均价23780</p>',None),
                           ('<p>登录后查看</p>',None)]:
        with pytest.raises(QuoteError) as caught:parse(content,article,known)
        assert not isinstance(caught.value,ArticleNotApplicable)
        assert diagnostic_reason(caught.value)=='history_section_missing'
    assert diagnostic_reason(RuntimeError('private source response'))=='source_request_or_processing_failed'




def test_daily_requests_only_home_and_date_reference(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch);original=commodities.fetch;seen=[]
    def fetch(client,url,catalog=False):
        seen.append(url)
        return original(client,url,catalog)
    monkeypatch.setattr(commodities,'fetch',fetch)
    for hour in (3,10):
        seen.clear()
        job.collect(job.lock(),datetime(2026,11,3,hour,tzinfo=timezone.utc))
        assert seen==[HOME,DATE_URL] and job.status()['mode']=='current'




def test_explicit_home_year_needs_one_request(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch);seen=[]
    def fetch(client,url,catalog=False):
        seen.append(url)
        assert url==HOME
        return HOME_HTML.replace('11月03日长江有色金属价格行情','2026年11月03日长江有色金属价格行情')
    monkeypatch.setattr(commodities,'fetch',fetch)
    job.collect(job.lock())
    assert seen==[HOME] and job.status()['currentVerified']


def test_failed_target_survives_restart_and_recovers_only_matching_quote(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch);original=commodities.fetch
    def failed(*args,**kwargs):raise TimeoutError('private')
    monkeypatch.setattr(commodities,'fetch',failed)
    job.collect(job.lock())
    assert job.status()['pendingTargets']==[{'day':'2026-11-03','reason':'sourceReadFailed'}]
    restarted=commodities.Commodities(job.settings) if hasattr(job,'settings') else commodities.Commodities(Settings(_env_file=None,log_dir=tmp_path))
    assert restarted.status()['pendingCount']==1
    monkeypatch.setattr(commodities,'fetch',original)
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,4,3,tzinfo=timezone.utc))
    restarted.collect(restarted.lock())
    assert restarted.history()['rows'][-1]['quoteDate']=='2026-11-03'
    assert restarted.status()['pendingTargets']==[{'day':'2026-11-04','reason':'sourceNotPublished'}]
    assert restarted.status()['currentVerified'] is False


def test_missing_prior_date_not_filled_with_later_price(tmp_path,monkeypatch):
    job=service(tmp_path,monkeypatch);original=commodities.fetch
    monkeypatch.setattr(commodities,'fetch',lambda *a,**k: (_ for _ in ()).throw(TimeoutError()))
    job.collect(job.lock())
    monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,4,3,tzinfo=timezone.utc))
    def later(client,url,catalog=False):
        return original(client,url,catalog).replace('11月03日','11月04日').replace('2026-11-03','2026-11-04')
    monkeypatch.setattr(commodities,'fetch',later)
    job.collect(job.lock())
    assert job.status()['pendingTargets']==[{'day':'2026-11-03','reason':'sourceReadFailed'}]
    assert not any(r['quoteDate']=='2026-11-03' for r in job.history()['rows'])
    assert job.status()['currentVerified']


@pytest.mark.parametrize('mode,expected', [('explicit','recovered'),('wrong','date_mismatch'),('relative','quote_date_missing'),('network','source_request_or_processing_failed')])
def test_one_targeted_homepage_review_only(tmp_path,monkeypatch,mode,expected):
    job=service(tmp_path,monkeypatch);original=commodities.fetch
    with job.db() as db:
        for day in ('2026-10-31','2026-11-01'):
            db.execute('INSERT INTO failed_targets VALUES(?,?,?)',(day,'sourceReadFailed','2026-11-01T00:00:00Z'))
    url='https://m.ccmn.cn/mnewsinfo/d5484f3e27d4438699d0972335e1403e.html'
    requests=[]
    def fetch(client,requested,catalog=False):
        requests.append(requested)
        if requested==HOME:return HOME_HTML+f'<a href="{url}">现铝早评</a>'
        if requested==url:
            if mode=='network':raise TimeoutError('private')
            day='10月30日' if mode=='wrong' else '' if mode=='relative' else '10月31日'
            return f'<p>2026-11-02</p><h2>五、长江市场行情</h2>{day}长江现货A00铝均价23780元/吨，涨40元/吨，价格区间23760-23800元/吨<h2>六、预测</h2>明日25000'
        return original(client,requested,catalog)
    monkeypatch.setattr(commodities,'fetch',fetch)
    job.collect(job.lock())
    assert requests==[HOME,DATE_URL,url]
    assert job.status()['recovery']=={'day':'2026-10-31','result':expected}
    assert job.status()['currentVerified']
    dates={r['quoteDate'] for r in job.history()['rows']}
    assert ('2026-10-31' in dates)==(mode=='explicit')
    assert job.status()['pendingCount']==(1 if mode=='explicit' else 2)
    # On failure the next run rotates to the other failed date, not a full scan.
    job.collect(job.lock())
    assert job.status()['recovery']['day']=='2026-11-01'
