"""Dated official CSI membership + signed Eastmoney PE_TTM. No fallback endpoints.

The latest workbook is usable only for its internal date. Cached proven snapshots
can support older dates; current rosters and monthly weights never fill history.
"""
from datetime import date
from decimal import Decimal
import hashlib
import json
import time

import httpx
import xlrd

from app.services.growth_dividend_source import CALENDAR_URL, decode_sina_trade_calendar
from app.services.valuation_ruler_model import START, PUBLIC_SOURCE, ValuationError, validate_day, valid_code

CSI_URL = 'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000300cons.xls'
PE_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
CLOSE_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
CALENDAR_SOURCE = 'SINA.TRADE_CALENDAR.V1'
MESSAGES = {
    'access_denied': '公开源拒绝访问或限流，本轮停止；请稍后检查来源状态。',
    'source_unavailable': '公开源暂不可用，本轮停止，已有数据保留。',
    'invalid_response': '公开源结构或数据校验失败，未发布该日。',
    'incomplete': '当日公开数据尚不完整，缺失日期保留待补。',
    'snapshot_unavailable': '缺少该日官方成分股快照；请等待当日快照或用聚宽补齐历史。',
    'budget_exceeded': '本轮达到安全时间或数据量上限，未完成日期保留待补。',
}


class SourceError(ValuationError):
    def __init__(self, code, *, retryable=False, common=False):
        self.code = code if code in MESSAGES else 'invalid_response'
        self.retryable, self.common = retryable, common
        super().__init__(MESSAGES[self.code])


def get_bytes(client, url, *, params=None, limit=2_000_000, check_cancel=lambda: None):
    deadline = time.monotonic()+90
    try:
        with client.stream('GET',url,params=params) as response:
            if response.status_code in (403,429):
                raise SourceError('access_denied',common=True)
            if response.status_code >= 400 or response.is_redirect:
                raise SourceError('source_unavailable',common=True)
            chunks,size = [],0
            for chunk in response.iter_bytes(65536):
                check_cancel()
                size += len(chunk)
                if size > limit or time.monotonic() > deadline:
                    raise SourceError('budget_exceeded',common=True)
                chunks.append(chunk)
        return b''.join(chunks)
    except httpx.HTTPError:
        raise SourceError('source_unavailable',common=True) from None


def json_payload(raw):
    try:
        payload = json.loads(raw,parse_float=Decimal,parse_constant=lambda value:Decimal(value))
    except (ValueError,UnicodeError):
        raise SourceError('invalid_response') from None
    if not isinstance(payload,dict):
        raise SourceError('invalid_response')
    return payload


def parse_snapshot(raw):
    if len(raw)>2_000_000:
        raise SourceError('invalid_response')
    try:
        book=xlrd.open_workbook(file_contents=raw,on_demand=True)
        try:
            sheet=book.sheet_by_index(0)
            if sheet.nrows!=301 or sheet.ncols>30:
                raise SourceError('invalid_response')
            columns=sheet.row_values(0)
            positions=[columns.index(field) for field in (
                '日期Date','指数代码 Index Code','成份券代码Constituent Code','交易所Exchange')]
            members,dates=set(),set()
            for index in range(1,sheet.nrows):
                row=[sheet.cell_value(index,pos) for pos in positions]
                stamp,code,member,exchange=row
                if not all(isinstance(value,str) for value in row) or code!='000300' or len(stamp)!=8:
                    raise SourceError('invalid_response')
                day=date.fromisoformat(stamp[:4]+'-'+stamp[4:6]+'-'+stamp[6:])
                if day<START or day.weekday()>=5:
                    raise SourceError('invalid_response')
                suffix={'上海证券交易所':'.XSHG','深圳证券交易所':'.XSHE'}.get(exchange)
                member=member+(suffix or '')
                if not suffix or not valid_code(member) or member in members:
                    raise SourceError('invalid_response')
                dates.add(day);members.add(member)
            if len(dates)!=1 or len(members)!=300:
                raise SourceError('invalid_response')
            day=dates.pop()
            return day,sorted(members),dict(source='CSI.OFFICIAL.CONS.V1',internal_date=str(day),
                workbook_sha256=hashlib.sha256(raw).hexdigest(),
                members_sha256=hashlib.sha256('\n'.join(sorted(members)).encode()).hexdigest())
        finally:
            book.release_resources()
    except SourceError:
        raise
    except Exception:
        raise SourceError('invalid_response') from None


def parse_pe_page(payload, day, page, *, expected=None):
    result=payload.get('result')
    # Permission/query/business errors are not assumed to mean data-not-ready.
    if payload.get('success') is not True or payload.get('code')!=0 or not isinstance(result,dict):
        raise SourceError('invalid_response',common=True)
    count,pages,rows=result.get('count'),result.get('pages'),result.get('data')
    if (type(count) is not int or type(pages) is not int or not 300<=count<=15000 or
            pages!=(count+4999)//5000 or not 1<=page<=pages or not isinstance(rows,list) or
            len(rows)!=min(5000,count-5000*(page-1)) or
            (expected is not None and (count,pages)!=expected)):
        raise SourceError('incomplete',retryable=True)
    values={}
    for row in rows:
        if not isinstance(row,dict):
            raise SourceError('invalid_response')
        code,secucode=row.get('SECURITY_CODE'),row.get('SECUCODE')
        if (not isinstance(code,str) or len(code)!=6 or not code.isdigit() or
                not isinstance(secucode,str) or secucode not in (code+'.SH',code+'.SZ',code+'.BJ') or
                row.get('TRADE_DATE')!=str(day)+' 00:00:00' or code in values or 'PE_TTM' not in row):
            raise SourceError('invalid_response')
        suffix={'.SH':'.XSHG','.SZ':'.XSHE','.BJ':'.XBSE'}[secucode[-3:]]
        values[code]=(code+suffix,row.get('PE_TTM'))
    return count,pages,values


def parse_close(payload,day):
    data=payload.get('data')
    if payload.get('rc')!=0 or not isinstance(data,dict) or data.get('code')!='000300' or data.get('market')!=1:
        raise SourceError('invalid_response')
    rows=data.get('klines')
    if not isinstance(rows,list) or len(rows)>10:
        raise SourceError('invalid_response')
    found=[]
    for row in rows:
        if not isinstance(row,str) or len(row.split(','))<3:
            raise SourceError('invalid_response')
        fields=row.split(',')
        if fields[0]==str(day):found.append(fields[2])
    if len(found)!=1:
        raise SourceError('incomplete',retryable=True)
    return found[0]


class PublicSource:
    configured=True

    def __init__(self,client,monitor,check_cancel):
        self.client,self.monitor,self.check_cancel=client,monitor,check_cancel
        self.latest=None

    def request(self,url,**kwargs):
        self.check_cancel()
        return get_bytes(self.client,url,check_cancel=self.check_cancel,**kwargs)

    def calendar(self,cutoff):
        raw=self.request(CALENDAR_URL,limit=400_000)
        try:days=decode_sina_trade_calendar(raw.decode('utf-8'))
        except (ValueError,UnicodeError):raise SourceError('invalid_response',common=True) from None
        if (not days or len(days)<5000 or days[0]>START or days[-1]<cutoff or
                days!=sorted(set(days)) or any(day.weekday()>=5 for day in days if day>=START)):
            raise SourceError('incomplete',common=True)
        return [day for day in days if day>=START]

    def snapshot(self,day):
        cached=self.monitor.load_snapshot(day)
        if cached:return cached
        if self.latest is None:
            raw=self.request(CSI_URL)
            self.latest=parse_snapshot(raw)
            self.monitor.save_snapshot(*self.latest,raw=raw)
        current,members,proof=self.latest
        if current!=day:raise SourceError('snapshot_unavailable')
        return members,proof

    def collect(self,day):
        members,proof=self.snapshot(day)
        values,page_hashes,expected={},[],None
        page,pages=1,1
        while page<=pages:
            raw=self.request(PE_URL,limit=8_000_000,params={
                'reportName':'RPT_VALUEANALYSIS_DET',
                'columns':'SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,PE_TTM,PE_LAR,CLOSE_PRICE,TOTAL_MARKET_CAP',
                'pageSize':'5000','pageNumber':str(page),'sortColumns':'SECURITY_CODE','sortTypes':'1',
                'filter':"(TRADE_DATE='{}')".format(day)})
            count,pages,rows=parse_pe_page(json_payload(raw),day,page,expected=expected)
            expected=count,pages
            if set(values)&set(rows):raise SourceError('incomplete',retryable=True)
            values.update(rows);page_hashes.append(hashlib.sha256(raw).hexdigest());page+=1
        if len(values)!=expected[0]:raise SourceError('incomplete',retryable=True)
        signed={}
        for code in members:
            found=values.get(code[:6])
            if found is None or found[0]!=code:
                raise SourceError('incomplete',retryable=True)
            signed[code]=found[1]
        raw=self.request(CLOSE_URL,limit=200_000,params={
            'secid':'1.000300','klt':'101','fqt':'0','beg':day.strftime('%Y%m%d'),'end':day.strftime('%Y%m%d'),
            'lmt':'10','fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
        close=parse_close(json_payload(raw),day)
        try:daily=validate_day(day,close,signed,PUBLIC_SOURCE)
        except ValuationError:raise SourceError('incomplete',retryable=True) from None
        proof=dict(proof,source=PUBLIC_SOURCE,pe_page_sha256=page_hashes,pe_row_count=len(values),
                   close_sha256=hashlib.sha256(raw).hexdigest())
        return daily,signed,proof
