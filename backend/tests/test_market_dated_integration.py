"""Synthetic offline dated-universe reconstruction tests; no captured source data."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from types import SimpleNamespace
import httpx
import pytest
from app.services.market_history import audit_historical_membership, category, DATED_SOURCE, DATED_MEMBERSHIP_BASIS
from app.services.market_public import PublicMarketProvider
from app.services.market_sina import MarketDependencyError, SinaDataError, SinaRateLimitError, aggregate_crowding, aggregate_heat, calculate_joinquant_quote
from test_market_public import DAYS, DAY
from test_market_history import inputs
from test_market_sina import qfq_arguments, SESSIONS

SYMBOLS = ['sh600001', 'sh689009', 'sz002623', 'sz302132'] + [f'sh{600100+n}' for n in range(96)]

def member(symbol, day):
    return {'symbol': symbol, 'name': '*ST CURRENT NAME', 'date': str(day), 'board': 'risk_warning_not_board',
            'evidence': {'source_id': DATED_SOURCE, 'report_date': str(day), 'name_basis': 'latest_vendor_backfill_not_historical_st'}}

def evidence(day):
    return {'source_id': DATED_SOURCE, 'evidence_version': 1, 'target_date': str(day), 'report_date_verified': True,
            'report_pages_complete': True, 'source_counts_verified': True, 'report_digest': 'b'*64,
            'membership_verified': False, 'listing_dates_verified': False}

def ipo_evidence(symbol, day):
    raw = json.dumps({'rc': 0, 'data': {'f13': 1 if symbol.startswith('sh') else 0, 'f57': symbol[2:],
        'f58': 'Current display name', 'f189': int(str(day).replace('-', ''))}})
    return {'symbol': symbol, 'exchange': 'SSE' if symbol.startswith('sh') else 'SZSE', 'ipo_date': str(day),
        'source_id': 'eastmoney_listing_metadata', 'evidence_version': 1,
        'request_secid': ('1.' if symbol.startswith('sh') else '0.')+symbol[2:], 'verified': True,
        'source_digest': sha256(raw.encode()).hexdigest(), 'source_schema': 'eastmoney_stock_get_f189_v1', 'raw_response': raw}

def make_dated(*, cache_root=None, absent=None, paused=None, failed=None, calendar=None, listing_metadata=None):
    calls = {key: [] for key in ('master', 'archive', 'count', 'pause')}
    absent, paused = absent or {}, paused or {}
    def active(day):
        return [symbol for symbol in SYMBOLS if day not in absent.get(symbol, [])]
    def master(day):
        calls['master'].append(day)
        if day == failed:
            raise SinaDataError('incomplete report', code='incomplete_page')
        rows = [member(symbol, day) for symbol in active(day)]
        return {'date': str(day), 'rows': rows, 'expected_count': len(rows), 'evidence': evidence(day)}
    def archive(day):
        calls['archive'].append(day)
        rows = [dict(symbol=symbol, date=str(day), close=str(10+DAYS.index(day)), volume='10', amount=str((i+1)*100))
                for i, symbol in enumerate(active(day)) if day not in paused.get(symbol, [])]
        return {'date': str(day), 'verified': True, 'rows': rows, 'source_digest': 'a'*64}
    def counts(day):
        calls['count'].append(day)
        values = dict.fromkeys(('sse_main_a', 'sse_star', 'szse_main_a', 'szse_chinext'), 0)
        for symbol in active(day):
            values[category(member(symbol, day))] += 1
        return {'date': str(day), 'verified': True, 'counts': values}
    def pauses(start, end):
        calls['pause'].append((start,end))
        return {'intervals': {symbol: [{'start': str(day), 'end': str(day), 'full_day': True} for day in days]
                              for symbol, days in paused.items()}}
    def no_ipo(symbol, day):
        raise SinaDataError('explicit IPO unavailable', code='dated_membership_gap')
    provider = PublicMarketProvider(httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No network'))),
        cache_root=cache_root, master=SimpleNamespace(dated_report=True, fetch=master), archives=SimpleNamespace(fetch=archive),
        counts=SimpleNamespace(fetch=counts), suspensions=SimpleNamespace(fetch=pauses),
        calendar_loader=lambda c: calendar if calendar is not None else DAYS,
        actions=SimpleNamespace(fetch=lambda: {'verified': True, 'coverage_through': str(DAY), 'rows': []}),
        listing_metadata=listing_metadata or SimpleNamespace(fetch=no_ipo))
    provider._prior_close = lambda symbol, day: (DAYS[0], Decimal('10'))
    return provider, calls

def dated_inputs():
    master, rows, raw, pauses, official = inputs()
    master['rows'] = [member(row['symbol'], master['date']) for row in rows]
    master['evidence'] = evidence(master['date'])
    return master, rows, raw, pauses, official

def test_dated_audit_no_fake_listing_or_direct_joinquant_identity():
    result = audit_historical_membership(*dated_inputs())
    assert result['membership_basis'] == DATED_MEMBERSHIP_BASIS
    assert result['historical_reconstruction_verified'] and not result['joinquant_universe_directly_verified']
    assert result['listing_dates_verified'] is False

@pytest.mark.parametrize('change', ['same_count_wrong_identity','outside_pause','wrong_date','missing_pages','version','digest','category_count'])
def test_audit_identity_and_category_guards(change):
    master, rows, raw, pauses, official = dated_inputs()
    if change == 'same_count_wrong_identity':
        raw['sh600999'] = dict(raw.pop('sh600001'), symbol='sh600999')
    elif change == 'outside_pause':
        pauses['intervals']['sz002999'] = [{'start': master['date'], 'full_day': True}]
    elif change == 'wrong_date': master['rows'][0]['date'] = '2026-11-05'
    elif change == 'missing_pages': master['evidence']['report_pages_complete'] = False
    elif change == 'version': master['evidence']['evidence_version'] = True
    elif change == 'digest': master['evidence']['report_digest'] = ''
    else: official['counts'].update(sse_main_a=2, sse_star=0)
    with pytest.raises(SinaDataError): audit_historical_membership(master, rows, raw, pauses, official)

@pytest.mark.parametrize('symbol,expected', [('sz302132','szse_chinext'),('sh689009','sse_star'),('sh688827','sse_star'),('sh605001','sse_main_a'),('sz003001','szse_main_a')])
def test_report_namespace_classifies_selected_members_not_industry_or_st(symbol, expected):
    assert category(member(symbol,DAY)) == expected

def test_unknown_selected_namespace_blocks_not_excludes():
    with pytest.raises(SinaDataError): category(member('sh699999',DAY))

def test_twenty_complete_audits_before_heat_keep_st_cdr_and_302():
    provider, calls = make_dated(listing_metadata=SimpleNamespace(fetch=lambda *args: pytest.fail('Mature stock needs no IPO')))
    snapshot = provider.fetch_universe(DAY)
    assert len(calls['master']) == 1 and aggregate_crowding(snapshot)['publishable']
    quotes = [provider.fetch_quote(row['symbol'],DAY,snapshot=row) for row in snapshot['rows']]
    assert aggregate_heat(snapshot,quotes)['publishable'] and len(calls['master']) == len(calls['count']) == 20
    assert all(quote['raw']['listing'] is None and quote['evidence']['listing_verified'] is False for quote in quotes)
    assert all('listed_from' not in row['listing'] for row in snapshot['rows'])

@pytest.mark.parametrize('missing',[DAYS[:5],[DAYS[5]],DAYS[:-1],[DAYS[0],DAYS[7]]])
def test_absence_never_implies_ipo_or_reentry(missing):
    provider,calls=make_dated(absent={'sh689009':missing})
    snapshot=provider.fetch_universe(DAY); row=next(row for row in snapshot['rows'] if row['symbol']=='sh689009')
    quote=provider.fetch_quote(row['symbol'],DAY,snapshot=row)
    assert quote['error']=='dated_membership_gap' and len(calls['count'])==20
    assert aggregate_crowding(snapshot)['publishable'] and not aggregate_heat(snapshot,[quote])['publishable']

@pytest.mark.parametrize('failure',['source','calendar','refusal'])
def test_shared_window_failure_keeps_target_crowding_ready(failure):
    provider,calls=make_dated(failed=DAYS[6] if failure=='source' else None,calendar=[DAY] if failure=='calendar' else None)
    snapshot=provider.fetch_universe(DAY)
    assert aggregate_crowding(snapshot)['publishable']
    if failure=='refusal': provider.counts.fetch=lambda day: (_ for _ in ()).throw(SinaRateLimitError(403))
    with pytest.raises((MarketDependencyError,SinaRateLimitError)):
        provider.fetch_quote(SYMBOLS[0],DAY,snapshot=snapshot['rows'][0])
    assert not provider._membership_windows

def test_pause_kept_in_pool_excluded_only_after_complete_window():
    provider,calls=make_dated(paused={'sz002623':[DAY]})
    snapshot=provider.fetch_universe(DAY); row=next(row for row in snapshot['rows'] if row['symbol']=='sz002623')
    quote=provider.fetch_quote(row['symbol'],DAY,snapshot=row)
    assert quote['status']=='paused' and quote['eligible'] is False and len(calls['count'])==20
    assert len(snapshot['rows'])==100

def test_exact_explicit_ipo_resolves_short_suffix_without_fake_interval():
    metadata=SimpleNamespace(fetch=lambda symbol,day:ipo_evidence(symbol,DAYS[5]))
    provider,calls=make_dated(absent={'sh689009':DAYS[:5]},listing_metadata=metadata)
    snapshot=provider.fetch_universe(DAY)
    quotes=[provider.fetch_quote(row['symbol'],DAY,snapshot=row) for row in snapshot['rows']]
    quote=next(q for q in quotes if q['symbol']=='sh689009')
    assert quote['status']=='insufficient_history' and quote['history_count']==15
    assert quote['ma20'] is None and not quote['eligible'] and 'listed_from' not in quote['raw']['listing']
    assert aggregate_heat(snapshot,quotes)['publishable'] and len(calls['count'])==20

@pytest.mark.parametrize('symbol,listed',[('sz302132','2010-08-27'),('sh689009','2020-10-29')])
def test_old_ipo_does_not_turn_code_change_into_new_listing(symbol,listed):
    provider,_=make_dated(absent={symbol:DAYS[:5]},listing_metadata=SimpleNamespace(fetch=lambda sym,day:ipo_evidence(sym,listed)))
    snapshot=provider.fetch_universe(DAY); row=next(row for row in snapshot['rows'] if row['symbol']==symbol)
    assert provider.fetch_quote(symbol,DAY,snapshot=row)['error']=='dated_membership_gap'

@pytest.mark.parametrize('location',['listing','listing_evidence','row_evidence','top_evidence','snapshot','version'])
def test_malformed_old_snapshot_returns_false(location):
    provider,_=make_dated(); snapshot=provider.fetch_universe(DAY)
    if location=='listing':snapshot['rows'][0]['listing']=None
    elif location=='listing_evidence':snapshot['rows'][0]['listing']['evidence']=None
    elif location=='row_evidence':snapshot['rows'][0]['universe_evidence']=None
    elif location=='top_evidence':snapshot['universe_evidence']=None
    elif location=='version':snapshot['universe_evidence']['evidence_version']=True
    else:snapshot=None
    assert provider.snapshot_compatible(snapshot) is False

def test_audit_cache_replay_and_warm_twenty_day_reuse(tmp_path):
    first,_=make_dated(cache_root=tmp_path); snapshot=first.fetch_universe(DAY)
    first.fetch_quote(SYMBOLS[0],DAY,snapshot=snapshot['rows'][0])
    second,calls=make_dated(cache_root=tmp_path); replay=second.fetch_universe(DAY)
    assert replay['rows']==snapshot['rows'] and replay['universe_evidence']==snapshot['universe_evidence']
    quote=second.fetch_quote(SYMBOLS[0],DAY,snapshot=replay['rows'][0])
    assert quote['status']=='current' and calls['master']==calls['count']==calls['pause']==[]
    assert len(calls['archive'])==20

@pytest.mark.parametrize('mode',['hash','rehash_identity','date','source','version'])
def test_audit_cache_corruption_rejected_without_same_attempt_fetch(tmp_path,mode):
    provider,_=make_dated(cache_root=tmp_path);provider.fetch_universe(DAY)
    path=provider._audit_path(DAY);payload=json.loads(path.read_bytes())
    if mode in ('hash','rehash_identity'):
        payload['inputs']['raw_equities'].pop(SYMBOLS[0])
        if mode=='rehash_identity':payload['sha256']=sha256(provider._audit_bytes(payload['inputs'])).hexdigest()
    elif mode=='date':payload['date']=str(DAYS[0])
    elif mode=='source':payload['source_id']='exchange_master'
    else:payload['version']=True
    path.write_text(json.dumps(payload)); second,calls=make_dated(cache_root=tmp_path)
    with pytest.raises(MarketDependencyError) as error:second.fetch_universe(DAY)
    assert error.value.code=='dated_report_cache_invalid' and calls['master']==[] and not path.exists()
    second.fetch_universe(DAY);assert calls['master']==[DAY]

@pytest.mark.parametrize('kind',['root','ancestor','file','dangling'])
def test_audit_cache_symlink_protection(tmp_path,kind):
    root=tmp_path/'root';target=tmp_path/'target';root.mkdir();target.mkdir()
    provider,calls=make_dated(cache_root=root);path=provider._audit_path(DAY)
    if kind=='root':path.parent.symlink_to(target,target_is_directory=True)
    elif kind=='ancestor':root.rmdir();root.symlink_to(target,target_is_directory=True)
    else:
        path.parent.mkdir();source=target/'data'
        if kind=='file':source.write_text('do not read or delete')
        path.symlink_to(source)
    with pytest.raises(MarketDependencyError):provider.fetch_universe(DAY)
    assert calls['master']==[]
    if kind=='file':assert source.read_text()=='do not read or delete' and path.is_symlink()

def test_failed_audit_never_caches_and_cancelled_atomic_write_cleans_temp(tmp_path,monkeypatch):
    from app.services.market_collection import CollectionCancelled
    import app.services.market_public as module
    provider,_=make_dated(cache_root=tmp_path)
    monkeypatch.setattr(module.os,'fsync',lambda fd:(_ for _ in ()).throw(CollectionCancelled()))
    with pytest.raises(CollectionCancelled):provider.fetch_universe(DAY)
    assert not list(tmp_path.rglob('*.json')) and not list(tmp_path.rglob('.audit-*'))

def pure_window():
    return {'symbol':'sh600519','source_id':DATED_SOURCE,'evidence_version':1,'target_date':str(SESSIONS[-1]),
            'sessions':[{'date':str(day),'present':True,'audited':True,'report_digest':'b'*64,'audit_digest':'a'*64} for day in SESSIONS]}

def pure_args(window,short=False):
    args=qfq_arguments();args.pop('listed_from');args.pop('listed_until')
    if short:args['rows']=args['rows'][5:]
    args['evidence'].update(listing_verified=False,dated_membership_verified=True,membership_window=window)
    return args

@pytest.mark.parametrize('change',['false','missing','unaudited','digest','source','interval'])
def test_pure_mask_rejects_incomplete_and_mixed_evidence(change):
    window=pure_window();args=pure_args(window)
    if change=='false':window['sessions'][0]['present']=False
    elif change=='missing':window['sessions'].pop()
    elif change=='unaudited':window['sessions'][0]['audited']=False
    elif change=='digest':window['sessions'][0]['audit_digest']='unknown'
    elif change=='source':window['source_id']='exchange_master'
    else:args['listed_from']=date(2000,1,1)
    with pytest.raises(SinaDataError):calculate_joinquant_quote('sh600519',SESSIONS[-1],membership_window=window,**args)

@pytest.mark.parametrize('change',['date','symbol','exchange','secid','verified','source','digest','version','schema','raw','missing_raw'])
def test_ipo_proof_strictly_binds_identity_date_and_raw_hash(change):
    window=pure_window()
    for record in window['sessions'][:5]:record['present']=False
    proof=ipo_evidence('sh600519',SESSIONS[5]);window['ipo_evidence']=proof
    mutations={'date':('ipo_date',str(SESSIONS[4])),'symbol':('symbol','sh600000'),'exchange':('exchange','SZSE'),
        'secid':('request_secid','0.600519'),'verified':('verified',False),'source':('source_id','clist'),
        'digest':('source_digest','unknown'),'version':('evidence_version',True),'schema':('source_schema','guessed'),
        'raw':('raw_response',proof['raw_response']+' '),'missing_raw':('raw_response',None)}
    key,value=mutations[change];proof[key]=value
    with pytest.raises(SinaDataError):calculate_joinquant_quote('sh600519',SESSIONS[-1],membership_window=window,**pure_args(window,True))

def test_pure_ipo_aggregate_rechecks_history_count_proof_and_snapshot_digest():
    window=pure_window()
    for record in window['sessions'][:5]:record['present']=False
    window['ipo_evidence']=ipo_evidence('sh600519',SESSIONS[5]);args=pure_args(window,True)
    quote=calculate_joinquant_quote('sh600519',SESSIONS[-1],membership_window=window,**args)
    assert quote['status']=='insufficient_history' and quote['history_count']==15 and quote['raw']['listing'] is None
    row=args['snapshot']|{'date':str(SESSIONS[-1]),'universe_evidence':{'source_id':DATED_SOURCE,'audit_digest':'a'*64,'report_digest':'b'*64}}
    universe={'date':str(SESSIONS[-1]),'expected_count':1,'rows':[row]}
    assert aggregate_heat(universe,[quote])['pending_count']==0
    for key,value in [('history_count',14),('evidence',{})]:
        bad=deepcopy(quote);bad[key]=value;assert aggregate_heat(universe,[bad])['pending_count']==1
    row['universe_evidence']['audit_digest']='d'*64
    assert aggregate_heat(universe,[quote])['pending_count']==1

@pytest.mark.parametrize('transport',[False,True])
def test_metadata_failure_scope_and_memo(transport):
    calls=[]
    def fail(symbol,day):
        calls.append((symbol,day))
        if transport:raise httpx.ConnectError('offline')
        raise SinaDataError('missing IPO field',code='invalid_response')
    provider,_=make_dated(listing_metadata=SimpleNamespace(fetch=fail))
    for symbol,day in [('sh689009',DAY),('sh689009',DAY),('sh689009',DAYS[-2]),('sz302132',DAY)]:
        with pytest.raises((SinaDataError,MarketDependencyError)):provider._ipo_metadata(symbol,day)
    assert len(calls)==(1 if transport else 3)

def test_semantic_digest_stable_across_delivery_metadata():
    provider,_=make_dated();first=provider.fetch_universe(DAY);original=provider.master.fetch
    def changed(day):
        result=original(day);result['evidence']['cache_hit']=True;result['raw']={'capture_time':'other'};return result
    provider.master.fetch=changed;second=provider.fetch_universe(DAY)
    assert first['universe_evidence']['audit_digest']==second['universe_evidence']['audit_digest']

def test_default_uses_dated_report_not_exchange_list_fallback(tmp_path):
    from app.services.market_eastmoney_universe import EastmoneyDatedUniverse
    provider=PublicMarketProvider(httpx.Client(transport=httpx.MockTransport(lambda r:pytest.fail('No network'))),cache_root=tmp_path)
    assert type(provider.master) is EastmoneyDatedUniverse and provider._dated_master
    assert provider.counts.__class__.__name__=='ExchangeDailyCounts'

def test_saved_quote_raw_proof_survives_replaceable_ipo_cache_eviction(tmp_path,monkeypatch):
    import app.services.market_eastmoney_listing as module
    from app.services.market_eastmoney_listing import EastmoneyListingMetadata,validate_listing_evidence
    monkeypatch.setattr(module,'MAX_CACHE_ENTRIES',1)
    def serve(request):
        market,code=request.url.params['secid'].split('.')
        return httpx.Response(200,json={'rc':0,'data':{'f13':int(market),'f57':code,'f58':'Current name','f189':int(str(DAYS[5]).replace('-',''))}})
    adapter=EastmoneyListingMetadata(httpx.Client(transport=httpx.MockTransport(serve)),cache_root=tmp_path)
    provider,_=make_dated(absent={'sh689009':DAYS[:5]},listing_metadata=adapter)
    snapshot=provider.fetch_universe(DAY);row=next(row for row in snapshot['rows'] if row['symbol']=='sh689009')
    quote=provider.fetch_quote(row['symbol'],DAY,snapshot=row);saved=json.dumps(quote,sort_keys=True)
    adapter.fetch('sz302132',DAY)
    assert not (adapter.cache_root/adapter._cache_name('sh689009')).exists()
    validate_listing_evidence(quote['evidence']['membership_window']['ipo_evidence'],'sh689009',DAY)
    assert json.dumps(quote,sort_keys=True)==saved
    assert aggregate_heat({'date':str(DAY),'expected_count':1,'rows':[row]},[quote])['pending_count']==0
