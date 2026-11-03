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
    if not ((catalog and url==CATALOG) or re.fullmatch(r'/(?:mnewsinfo/[a-f0-9]{29,32}|news/ZX003/\d{6}/[a-f0-9]{29,32})\.html',p.path)):
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
