"""Synthetic offline transport/workbook tests, not a replay of a live CSI file.

The earlier public probe files were lost during workspace recovery. These tests
must never be described as current proof of real-source data or vendor equality.
"""
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import valuation_ruler_source as s

DAY=date(2026,11,10)
RAW=b'synthetic workbook transport marker, not a real xls'
HEADERS=['日期Date','指数代码 Index Code','成份券代码Constituent Code','交易所Exchange']


def roster_rows():
    return [[DAY.strftime('%Y%m%d'),'000300',f'{i:06d}','深圳证券交易所'] for i in range(1,301)]


def install_book(monkeypatch,rows=None,columns=None):
    """A synthetic xlrd workbook mock tests field/date/identity gates only."""
    rows=roster_rows() if rows is None else rows
    columns=HEADERS if columns is None else columns
    class Sheet:
        nrows=len(rows)+1
        ncols=len(columns)
        def row_values(self,index):return columns
        def cell_value(self,index,pos):return rows[index-1][pos]
    book=SimpleNamespace(sheet_by_index=lambda _:Sheet(),release_resources=lambda:None)
    monkeypatch.setattr(s.xlrd,'open_workbook',lambda **kwargs:book)


def pe_rows(count=300):
    return [dict(SECURITY_CODE=f'{i:06d}',SECUCODE=f'{i:06d}.SZ',TRADE_DATE=str(DAY)+' 00:00:00',
        PE_TTM='-5.125' if i%12==0 else '10.000000000001') for i in range(1,count+1)]


def envelope(rows,count=None,pages=None):
    count=len(rows) if count is None else count
    return dict(success=True,code=0,result=dict(count=count,pages=pages or (count+4999)//5000,data=rows))


def close_payload():
    return dict(rc=0,data=dict(code='000300',market=1,name='沪深300',klines=[str(DAY)+',4000.00,4059.11,4100,3999']))


def test_workbook_exact_schema_internal_date_and_300_unique_synthetic_members(monkeypatch):
    install_book(monkeypatch)
    day,members,proof=s.parse_snapshot(RAW)
    assert day==DAY and len(members)==300 and members[0]=='000001.XSHE'
    assert proof['internal_date']==str(DAY) and proof['workbook_sha256']==hashlib.sha256(RAW).hexdigest()
    assert proof['members_sha256']==hashlib.sha256('\n'.join(members).encode()).hexdigest()


@pytest.mark.parametrize('mutate',[
    lambda rows:rows.pop(),lambda rows:rows.append(rows[0]),lambda rows:rows.__setitem__(1,rows[0]),
    lambda rows:rows[0].__setitem__(0,'20261109'),lambda rows:rows[0].__setitem__(0,'20261107'),
    lambda rows:rows[0].__setitem__(1,'000905'),lambda rows:rows[0].__setitem__(2,'600001'),
    lambda rows:rows[0].__setitem__(3,'unknown'),lambda rows:rows[0].__setitem__(2,1),
])
def test_workbook_rejects_missing_duplicate_wrong_index_date_exchange_or_format(monkeypatch,mutate):
    rows=roster_rows();mutate(rows);install_book(monkeypatch,rows)
    with pytest.raises(s.SourceError):s.parse_snapshot(RAW)


def test_workbook_malformed_bytes_and_size_limit():
    for raw in (b'not an excel',b'x'*2_000_001):
        with pytest.raises(s.SourceError):s.parse_snapshot(raw)


@pytest.mark.parametrize('mutate',[
    lambda p:p['result'].update(count=301),lambda p:p['result'].update(pages=2),
    lambda p:p['result']['data'][0].update(TRADE_DATE='2026-11-09 00:00:00'),
    lambda p:p['result']['data'].__setitem__(1,dict(p['result']['data'][0])),
    lambda p:p['result']['data'][0].update(SECUCODE='000001.SHX'),
])
def test_pe_date_count_pagination_and_unique_identity_gates(mutate):
    payload=envelope(pe_rows());mutate(payload)
    with pytest.raises(s.SourceError):s.parse_pe_page(payload,DAY,1)


def test_business_errors_common_nonretryable_not_assumed_incomplete():
    for payload in ({},dict(code=9201,success=False,result=None),dict(code=999,success=False)):
        with pytest.raises(s.SourceError) as error:s.parse_pe_page(payload,DAY,1)
        assert error.value.common and not error.value.retryable


def test_all_pages_exact_lengths_stable_count_unique_full_population():
    rows=pe_rows(5580)
    count,pages,first=s.parse_pe_page(envelope(rows[:5000],5580,2),DAY,1)
    _,_,second=s.parse_pe_page(envelope(rows[5000:],5580,2),DAY,2,expected=(count,pages))
    assert len(first)+len(second)==5580 and not set(first)&set(second)
    with pytest.raises(s.SourceError):s.parse_pe_page(envelope(rows[5000:-1],5580,2),DAY,2,expected=(count,pages))
    with pytest.raises(s.SourceError):s.parse_pe_page(envelope(pe_rows()),DAY,1,expected=(301,1))
    with pytest.raises(s.SourceError):s.parse_pe_page(envelope(pe_rows()),DAY,2)


@pytest.mark.parametrize('mutate',[
    lambda p:p['data'].update(code='399300'),lambda p:p['data'].update(market=0),lambda p:p.update(rc=1),
    lambda p:p['data'].update(klines=[]),lambda p:p['data']['klines'].append(p['data']['klines'][0]),
])
def test_raw_index_close_identity_date_and_duplicates(mutate):
    payload=close_payload();mutate(payload)
    with pytest.raises(s.SourceError):s.parse_close(payload,DAY)


@pytest.mark.parametrize('status',[403,429,500,302])
def test_http_denials_never_retry_redirect_or_fallback(status):
    calls=[]
    def response(request):calls.append(request);return httpx.Response(status,content=b'private upstream body')
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(s.SourceError) as error:s.get_bytes(client,s.CSI_URL)
    assert len(calls)==1 and error.value.common
    assert error.value.code==('access_denied' if status in (403,429) else 'source_unavailable')
    assert 'private' not in str(error.value)


def test_bounded_stream_and_cancel():
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'x'*101))) as client:
        with pytest.raises(s.SourceError):s.get_bytes(client,s.CSI_URL,limit=100)
        def cancel():raise RuntimeError('cancelled')
        with pytest.raises(RuntimeError):s.get_bytes(client,s.CSI_URL,check_cancel=cancel)


def test_http_timeout_common_no_message_leak():
    def response(request):raise httpx.ReadTimeout('password=secret')
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(s.SourceError) as error:s.get_bytes(client,s.CSI_URL)
    assert error.value.common and 'secret' not in str(error.value)


def test_latest_snapshot_cached_under_internal_date_even_if_target_mismatch(monkeypatch):
    install_book(monkeypatch)
    class Store:
        def __init__(self):self.saved={}
        def load_snapshot(self,day):return self.saved.get(day)
        def save_snapshot(self,day,members,proof,raw):self.saved[day]=(members,proof)
    store=Store();calls=[]
    def response(request):calls.append(request);return httpx.Response(200,content=RAW)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source=s.PublicSource(client,store,lambda:None)
        with pytest.raises(s.SourceError):source.snapshot(DAY-timedelta(days=1))
        assert len(source.snapshot(DAY)[0])==300
        with pytest.raises(s.SourceError):source.snapshot(DAY+timedelta(days=1))
    assert len(calls)==1 and DAY in store.saved


def test_whole_public_flow_synthetic_300_signed_values_and_proof(monkeypatch):
    install_book(monkeypatch);calls=[]
    class Store:
        def load_snapshot(self,day):return None
        def save_snapshot(self,*args,**kwargs):pass
    def response(request):
        calls.append(request.url.host)
        if request.url.host=='oss-ch.csindex.com.cn':return httpx.Response(200,content=RAW)
        if request.url.host=='datacenter-web.eastmoney.com':return httpx.Response(200,json=envelope(pe_rows()))
        return httpx.Response(200,json=close_payload())
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        daily,members,proof=s.PublicSource(client,Store(),lambda:None).collect(DAY)
    assert daily['positive_count']==275 and daily['loss_count']==25
    assert daily['mean_pe_ttm']=='10.000000000001' and daily['index_close']=='4059.11000000'
    assert len(members)==300 and len(proof['pe_page_sha256'])==1 and len(calls)==3


def test_absent_pe_not_silently_excluded_and_close_not_requested(monkeypatch):
    install_book(monkeypatch);rows=pe_rows();rows[0]['PE_TTM']=None;calls=[]
    class Store:
        def load_snapshot(self,day):
            _,members,proof=s.parse_snapshot(RAW);return members,proof
    def response(request):calls.append(request);return httpx.Response(200,json=envelope(rows))
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(s.SourceError) as error:s.PublicSource(client,Store(),lambda:None).collect(DAY)
    assert error.value.code=='incomplete' and len(calls)==1


def test_full_flow_duplicate_cross_page_rejected(monkeypatch):
    install_book(monkeypatch);rows=pe_rows(5580);calls=[]
    class Store:
        def load_snapshot(self,day):
            _,members,proof=s.parse_snapshot(RAW);return members,proof
    def response(request):
        calls.append(request)
        page=int(request.url.params['pageNumber'])
        subset=rows[:5000] if page==1 else [rows[0]]+rows[5001:]
        return httpx.Response(200,json=envelope(subset,5580,2))
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(s.SourceError) as error:s.PublicSource(client,Store(),lambda:None).collect(DAY)
    assert error.value.code=='incomplete' and len(calls)==2


def test_decimal_json_parsing_never_converts_pe_via_binary_float():
    payload=s.json_payload(b'{"value":999999999999999999.999999999999}')
    assert payload['value']==Decimal('999999999999999999.999999999999')
    with pytest.raises(s.SourceError):s.json_payload(b'not json')
