"""Strict CCMN historical A00 quote adapter; never parse forecast section six."""
from datetime import date
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit, urljoin

HOSTS = {'www.ccmn.cn','m.ccmn.cn','alu.ccmn.cn','pb.ccmn.cn','copper.ccmn.cn'}
CATALOG = 'https://m.ccmn.cn/al/alxinwen/'
START = '2026-10-08'

class QuoteError(ValueError):
    pass

class Document(HTMLParser):
    def __init__(self, content):
        super().__init__(); self.parts=[]; self.links=[]; self.hidden=0; self.feed(content)
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden+=1
        if tag=='a':
            value=dict(attrs).get('href')
            if value:self.links.append(value)
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)
    @property
    def text(self):return re.sub(r'\s+','',''.join(self.parts))

def safe_url(url, catalog=False):
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname not in HOSTS or p.port not in (None,443) or p.username or p.password or p.query or p.fragment:
        raise QuoteError('不支持的公开来源地址')
    if not ((catalog and url in (CATALOG, 'https://m.ccmn.cn/')) or re.fullmatch(r'/(?:mnewsinfo/[a-f0-9]{29,32}|news/ZX003/\d{6}/[a-f0-9]{29,32})\.html',p.path)):
        raise QuoteError('不是受支持的公开历史正文')
    return url

def fetch(client,url,catalog=False):
    safe_url(url,catalog)
    with client.stream('GET',url) as r:
        r.raise_for_status()  # redirects/login/paywall are not followed or bypassed
        chunks=[]; size=0
        for chunk in r.iter_bytes():
            size+=len(chunk)
            if size>2_000_000:raise QuoteError('来源正文过大')
            chunks.append(chunk)
        return b''.join(chunks).decode(r.encoding or 'utf-8',errors='replace')

def discover(content):
    urls=[]
    for href in Document(content).links:
        url=urljoin(CATALOG,href)
        try:safe_url(url)
        except (ValueError,TypeError):continue
        if url not in urls:urls.append(url)
    return urls[:60]

def parse(content,url,known=None):
    safe_url(url)
    raw=Document(content).text
    part=re.search(r'五[、.．]长江市场行情(.*?)六[、.．]',raw)
    if not part:raise QuoteError('未找到历史行情段落，保留已有记录')
    pub=re.search(r'(20\d{2}-\d{2}-\d{2})',raw[:part.start()])
    if not pub:raise QuoteError('无法确认正文发布日期')
    published=date.fromisoformat(pub[1])
    section=part[1]
    quote=re.search(r'(?:(\d{1,2})月(\d{1,2})日)?长江现货A00铝均价([\d,]+)(?:元/吨)?[，,](涨|跌|持平)(\d+)?(?:元/吨|元)?[^。；;]{0,45}?([\d,]+)[-—~～]([\d,]+)元/吨',section)
    if not quote:raise QuoteError('未找到明确的A00现货历史区间')
    if known:
        if published.isoformat()!=known['publishedDate']:raise QuoteError('正文日期与已核验来源不符')
        day=date.fromisoformat(known['quoteDate'])
    elif quote[1]:
        year=published.year-(int(quote[1])>published.month)
        day=date(year,int(quote[1]),int(quote[2]))
    else:raise QuoteError('历史行情未注明报价日期，拒绝推算上一交易日')
    if quote[1] and (day.month,day.day)!=(int(quote[1]),int(quote[2])):raise QuoteError('报价日期冲突')
    if not START<=day.isoformat()<published.isoformat():raise QuoteError('报价日期不属于已发布历史')
    low,high,mean=[int(v.replace(',','')) for v in (quote[6],quote[7],quote[3])]
    if not 0<low<=mean<=high<1_000_000:raise QuoteError('价格区间校验失败')
    change=int(quote[5] or 0)*(-1 if quote[4]=='跌' else 1)
    return dict(quoteDate=day.isoformat(),low=low,high=high,mean=mean,change=change,publishedDate=published.isoformat(),sourceUrl=url,rangeEvidence='page',sourceSection='五、长江市场行情')

# Current public homepage quote. The instrument/market URL is part of the
# public anchor, so another market's A00 row cannot be selected by row order.
HOME = 'https://m.ccmn.cn/'
A00_PATH = '/mhangqing/chart/cjxh/40288092327157530132716d2960000c'


class QuoteIncomplete(QuoteError):
    pass


class HomeDocument(Document):
    def __init__(self, content):
        self.anchors = []
        self.anchor = None
        super().__init__(content)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == 'a':
            self.anchor = [dict(attrs).get('href', ''), []]

    def handle_data(self, data):
        super().handle_data(data)
        if self.anchor is not None and not self.hidden:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.anchors.append((self.anchor[0], ' '.join(self.anchor[1])))
            self.anchor = None
        super().handle_endtag(tag)


def home_date_reference(content):
    """Find the matching public daily report metadata; never read its paid quote."""
    doc = HomeDocument(content)
    heading = re.search(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日长江有色金属价格行情', doc.text)
    if not heading:
        raise QuoteError('首页缺少长江报价日期标题')
    month, day = map(int, heading.group(2, 3))
    matches = set()
    for href, label in doc.anchors:
        match = re.fullmatch(r'\s*(\d{1,2})月(\d{1,2})日长江有色金属现货行情(?:\s+\d{2}-\d{2})?\s*', label)
        if match and tuple(map(int, match.group(1, 2))) == (month, day):
            url = urljoin(HOME, href)
            safe_url(url)
            matches.add(url)
    if len(matches) != 1:
        raise QuoteError('首页报价年份无法确认，未找到唯一同日公开日期信息')
    return matches.pop()


def public_report_date(content):
    # Only the public title and immediately following publication metadata are
    # used. Missing/login-only metadata is rejected; no protected body fallback.
    raw = Document(content).text
    match = re.search(r'(\d{1,2})月(\d{1,2})日长江有色金属现货行情.{0,80}?(20\d{2}-\d{2}-\d{2})', raw)
    if not match:
        raise QuoteError('无法核验公开报价日期信息')
    published = date.fromisoformat(match[3])
    if (published.month, published.day) != tuple(map(int, match.group(1, 2))):
        raise QuoteError('公开标题与发布日期不一致')
    return published


def parse_home(content, *, today, report_date, report_url):
    doc = HomeDocument(content)
    heading = re.search(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日长江有色金属价格行情', doc.text)
    if not heading or (report_date.month, report_date.day) != tuple(map(int, heading.group(2, 3))):
        raise QuoteError('首页与公开日期信息不一致，保留历史')
    if heading[1] and int(heading[1]) != report_date.year:
        raise QuoteError('首页报价年份不一致')
    if not START <= report_date.isoformat() <= today.isoformat():
        raise QuoteError('首页报价日期超出范围')
    safe_url(report_url)
    rows = [label for href, label in doc.anchors if urljoin(HOME, href) == HOME.rstrip('/') + A00_PATH]
    if len(rows) != 1:
        raise QuoteError('首页缺少唯一长江现货A00报价')
    # This identified CCMN spot instrument is CNY/tonne, as in the historical
    # series. Do not accept a row explicitly labelled with another currency/unit.
    match = re.fullmatch(r'\s*A00铝\s+([\d,]+)\s*[-—~～]\s*([\d,]+)\s+([\d,]+)\s+([+-]?[\d,]+)\s*', rows[0])
    if not match:
        raise QuoteIncomplete('首页长江现货A00字段或单位不符合预期')
    low, high, mean, change = [int(x.replace(',', '')) for x in match.groups()]
    if not 0 < low <= mean <= high < 1_000_000 or abs(change) >= 1_000_000:
        raise QuoteError('首页报价区间无效')
    return dict(quoteDate=report_date.isoformat(), low=low, high=high, mean=mean, change=change,
                publishedDate=report_date.isoformat(), sourceUrl=HOME, dateSourceUrl=report_url,
                rangeEvidence='page', sourceSection='首页长江现货A00铝')
