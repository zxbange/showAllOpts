"""Strict CCMN historical A00 quote adapter; never parse forecast section six."""
from datetime import date
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit, urljoin

HOSTS = {'www.ccmn.cn','m.ccmn.cn','alu.ccmn.cn','pb.ccmn.cn','copper.ccmn.cn'}
CATALOG = 'https://m.ccmn.cn/al/alxinwen/'
ALU_HOME = 'https://alu.ccmn.cn/'
CNMN_CATALOG = 'https://www.cnmn.com.cn/ShowNewsList.aspx?id=95410'
START = '2026-10-08'

class QuoteError(ValueError):
    pass

class ArticleNotApplicable(QuoteError):
    """Discovered public article has no supported A00 history evidence."""


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
    # A public daily table with explicit CCMN attribution, not a hidden CCMN endpoint.
    if p.scheme == 'https' and p.netloc == 'www.cnmn.com.cn' and not p.fragment:
        if (catalog and url == CNMN_CATALOG) or (p.path == '/ShowNews1.aspx' and re.fullmatch(r'id=[0-9]{1,10}', p.query)):
            return url
        raise QuoteError('不支持的公开日报地址')
    if p.scheme!='https' or p.hostname not in HOSTS or p.port not in (None,443) or p.username or p.password or p.query or p.fragment:
        raise QuoteError('不支持的公开来源地址')
    if not ((catalog and url in (CATALOG, 'https://m.ccmn.cn/', ALU_HOME)) or re.fullmatch(r'/(?:mnewsinfo/[a-f0-9]{29,32}|news/ZX(?:003|018)/\d{6}/[a-f0-9]{29,32})\.html',p.path)):
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
        body = b''.join(chunks)
        if urlsplit(url).hostname == 'www.cnmn.com.cn':
            # Respect declared Chinese encodings on the public daily pages;
            # an undecodable response must fail validation rather than invent data.
            charset = re.search(rb'''charset\s*=\s*["']?([A-Za-z0-9_-]+)''', body[:8192], re.I)
            encoding = charset[1].decode('ascii').lower() if charset else (r.encoding or 'utf-8')
            if encoding.lower().replace('_','-') not in ('utf-8','utf8','utf-8-sig','gb2312','gbk','gb18030'):
                raise QuoteError('日报字符编码不受支持')
            try:
                return body.decode(encoding, errors='strict')
            except UnicodeError:
                raise QuoteError('日报字符编码无效') from None
        return body.decode(r.encoding or 'utf-8',errors='replace')

def discover(content):
    urls=[]
    for href, label in HomeDocument(content).anchors:
        # Public directories mix metals and generic industry news. Keep aluminum
        # candidates; the separate homepage date lookup still reads daily metadata.
        if not re.search(r'A00|铝', label):
            continue
        url=urljoin(CATALOG,href)
        try:safe_url(url)
        except (ValueError,TypeError):continue
        if urlsplit(url).hostname in HOSTS and url not in urls:urls.append(url)
    return urls[:60]

def parse(content,url,known=None):
    safe_url(url)
    raw=Document(content).text
    part=re.search(r'五[、.．]长江市场行情(.*?)六[、.．]',raw)
    if not part:
        title = re.search(r'<h1\b[^>]*>(.*?)</h1>', content, re.I | re.S)
        unrelated_title = title is not None and not re.search(r'A00|现铝|铝价|行情|早评', Document(title[1]).text)
        if known is None and unrelated_title and 'A00' not in raw and '长江市场行情' not in raw:
            raise ArticleNotApplicable('发现文章不含A00历史行情')
        raise QuoteError('未找到历史行情段落，保留已有记录')
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
    if quote[4] != '持平' and quote[5] is None:
        raise QuoteError('涨跌字段缺失')
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


def home_date_reference(content, *, references=None, base=HOME):
    """Find the matching public daily report metadata; never read its paid quote."""
    doc = HomeDocument(content)
    heading = re.search(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日长江有色金属价格行情', doc.text)
    if not heading:
        raise QuoteError('首页缺少长江报价日期标题')
    month, day = map(int, heading.group(2, 3))
    matches = set()
    for href, label in (doc.anchors if references is None else HomeDocument(references).anchors):
        match = re.fullmatch(r'\s*(\d{1,2})月(\d{1,2})日长江有色金属现货行情(?:\s+(?:20\d{2}-)?\d{2}-\d{2})?\s*', label)
        if match and tuple(map(int, match.group(1, 2))) == (month, day):
            url = urljoin(base, href)
            try: safe_url(url)
            except (ValueError, TypeError): continue
            if urlsplit(url).hostname in HOSTS:
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
    safe_url(report_url, catalog=report_url == HOME)
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


def diagnostic_reason(error):
    # Only fixed parser messages map to public codes; never echo upstream text.
    if isinstance(error, ArticleNotApplicable):
        return 'article_not_applicable'
    if isinstance(error, QuoteError):
        return {
            '未找到历史行情段落，保留已有记录': 'history_section_missing',
            '首页缺少长江报价日期标题': 'home_date_heading_missing',
            '首页报价年份无法确认，未找到唯一同日公开日期信息': 'home_date_reference_missing',
            '无法核验公开报价日期信息': 'public_report_date_missing',
            '公开标题与发布日期不一致': 'public_report_date_conflict',
            '无法确认正文发布日期': 'publication_date_missing',
            '未找到明确的A00现货历史区间': 'spot_range_missing',
            '历史行情未注明报价日期，拒绝推算上一交易日': 'quote_date_missing',
            '正文日期与已核验来源不符': 'publication_date_conflict',
            '报价日期冲突': 'quote_date_conflict',
            '报价日期不属于已发布历史': 'quote_date_out_of_range',
            '价格区间校验失败': 'invalid_range',
        }.get(str(error), 'quote_validation_failed')
    return 'source_request_or_processing_failed'


def explicit_home_date(content):
    """Use one request only when the source itself supplies a complete date."""
    match = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日长江有色金属价格行情', HomeDocument(content).text)
    return date(*map(int, match.groups())) if match else None


_CNMN_TITLE = r'(20[0-9]{2})年([0-9]{1,2})月([0-9]{1,2})日长江有色金属现货行情'


def cnmn_daily_links(content, *, targets):
    """Find only dated public daily reports matching requested trading dates."""
    matches = {}
    for href, label in HomeDocument(content).anchors:
        title = re.fullmatch(r'\s*' + _CNMN_TITLE + r'\s*', re.sub(r'\s+', '', label))
        if title is None:
            continue
        try:
            stamp = date(*map(int, title.groups())).isoformat()
            url = urljoin(CNMN_CATALOG, href)
            safe_url(url)
        except (ValueError, TypeError):
            continue
        if stamp in targets and urlsplit(url).hostname == 'www.cnmn.com.cn':
            matches.setdefault(stamp, set()).add(url)
    return {day: next(iter(urls)) for day, urls in matches.items() if len(urls) == 1}


class DailyTable(Document):
    def __init__(self, content):
        self.tables, self.rows, self.row, self.cell = [], None, None, None
        super().__init__(content)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == 'table':
            self.rows = []
        elif tag == 'tr' and self.rows is not None:
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        super().handle_data(data)
        if self.cell is not None and not self.hidden:
            self.cell.append(data)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r'\s+', '', ''.join(self.cell)))
            self.cell = None
        elif tag == 'tr' and self.row is not None and self.rows is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == 'table' and self.rows is not None:
            self.tables.append(self.rows)
            self.rows, self.row, self.cell = None, None, None


def parse_cnmn_daily(content, url, *, expected, today):
    safe_url(url)
    if urlsplit(url).hostname != 'www.cnmn.com.cn':
        raise QuoteError('不是中国有色网公开日报')
    doc = DailyTable(content)
    raw = doc.text
    # Heading immediately followed by full public publication date/time. Do
    # not accept titles from related links, an inferred year or other markets.
    stamp = re.search(_CNMN_TITLE + r'(20[0-9]{2})年([0-9]{1,2})月([0-9]{1,2})日[0-9]{1,2}:[0-9]{1,2}', raw)
    if not stamp:
        raise QuoteError('日报标题或发布日期无法核验')
    try:
        quote = date(*map(int, stamp.groups()[:3]))
        published = date(*map(int, stamp.groups()[3:]))
    except ValueError:
        raise QuoteError('日报日期无效') from None
    if quote != published or quote.isoformat() != expected or not START <= expected <= today.isoformat():
        raise QuoteError('日报报价日期与目标不一致')
    if not re.search(r'来源[：:]长江有色金属网(?:www\.)?ccmn\.cn', raw, re.I) or not re.search(r'单位[：:]元[，,]含税13%', raw):
        raise QuoteError('日报未明确标注CCMN来源和含税单位')
    header = ['品种', '材质', '最低价', '最高价', '均价', '涨跌']
    tables = [table for table in doc.tables if header in table]
    rows = [row for table in tables for row in table if row and row[0] == 'A00铝']
    if len(tables) != 1 or len(rows) != 1 or len(rows[0]) != 6 or rows[0][1] != 'A00':
        raise QuoteError('日报缺少唯一完整A00现货报价')
    prices = rows[0][2:]
    if any(not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)', value) for value in prices):
        raise QuoteError('日报报价字段不完整')
    low, high, mean, change = [int(value.replace(',', '')) for value in prices]
    if not 0 < low <= mean <= high < 1_000_000 or abs(change) >= 1_000_000:
        raise QuoteError('日报价格区间校验失败')
    return dict(quoteDate=expected, low=low, high=high, mean=mean, change=change,
                publishedDate=published.isoformat(), sourceUrl=url,
                rangeEvidence='page', sourceSection='中国有色网公开日报（注明CCMN来源）')


def homepage_review_link(content):
    """One public review linked by the already fetched homepage; no directory scan."""
    for href, label in HomeDocument(content).anchors:
        if not re.search(r'现铝|铝早评|铝早报', label):
            continue
        url = urljoin(HOME, href)
        try:
            safe_url(url)
        except ValueError:
            continue
        return url
    return None


def recovery_links(content, *, start, end, base=CATALOG):
    """Only follow public aluminum links in the requested recovery interval.

    A catalog is a recent-article list, not a complete historical date API.
    Undated links remain candidates; their body must prove an actual quote date.
    """
    urls = []
    for href, label in HomeDocument(content).anchors:
        if not re.search(r'A00|铝', label):
            continue
        stamp = re.search(r'20\d{2}-\d{2}-\d{2}', label)
        if stamp and not start <= stamp[0] <= end:
            continue
        url = urljoin(base, href)
        try:
            safe_url(url)
        except (ValueError, TypeError):
            continue
        if urlsplit(url).hostname in HOSTS and url not in urls:
            urls.append(url)
    return urls[:60]


def parse_recovery(content, url, *, today):
    """Parse either verified historical layout, never prices from a forecast.

    The public 2026-11-04 review moved dated spot evidence into section two.
    Require that section's explicit month/day, market, price, change and range.
    """
    try:
        row = parse(content, url)
    except QuoteError:
        safe_url(url)
        raw = Document(content).text
        part = re.search(r'二[、.．]现货(.*?)三[、.．]', raw)
        if not part:
            raise
        pub = re.search(r'(20\d{2}-\d{2}-\d{2})', raw[:part.start()])
        if not pub:
            raise QuoteError('无法确认正文发布日期')
        published = date.fromisoformat(pub[1])
        quote = re.search(
            r'(\d{1,2})月(\d{1,2})日长江现货A00铝均价([\d,]+)元/吨'
            r'[，,](?:较前交易日)?(上涨|下跌|涨|跌|持平)(\d+)?(?:元/吨|元)?'
            r'[，,]价格区间([\d,]+)[-—~～]([\d,]+)元(?:/吨)?', part[1])
        if not quote:
            raise QuoteError('未找到明确的A00现货历史区间')
        month, day = map(int, quote.group(1, 2))
        quoted = date(published.year - (month > published.month), month, day)
        low, high, mean = [int(v.replace(',', '')) for v in quote.group(6, 7, 3)]
        if not START <= quoted.isoformat() < published.isoformat():
            raise QuoteError('报价日期不属于已发布历史')
        if not 0 < low <= mean <= high < 1_000_000:
            raise QuoteError('价格区间校验失败')
        if quote[4] != '持平' and quote[5] is None:
            raise QuoteError('涨跌字段缺失')
        change = int(quote[5] or 0) * (-1 if quote[4] in ('跌', '下跌') else 1)
        row = dict(quoteDate=quoted.isoformat(), low=low, high=high, mean=mean,
                   change=change, publishedDate=published.isoformat(), sourceUrl=url,
                   rangeEvidence='page', sourceSection='二、现货历史行情')
    if row['publishedDate'] > today.isoformat() or row['quoteDate'] > today.isoformat():
        raise QuoteError('报价日期不属于已发布历史')
    return row
