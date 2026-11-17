"""Bounded, credential-free social image extraction and pinned HTTPS downloads."""
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

MAX_IMAGES = 18
MAX_BYTES = 10 * 1024 * 1024
HOSTS = {'weibo': ('sinaimg.cn', 'sinaimg.com'), 'xueqiu': ('xqimg.com', 'imedao.com')}


def image_url(value, platform):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    value = html.unescape(value).strip()
    if value.startswith('//'):
        value = 'https:' + value
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
                or any(c.isspace() or ord(c) < 32 for c in value)
                or not any(host == suffix or host.endswith('.' + suffix) for suffix in HOSTS.get(platform, ()))):
            return None
        return urlunsplit(('https', host, url.path or '/', url.query, ''))
    except ValueError:
        return None


class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag != 'img':
            return
        attrs = dict(attrs)
        marker = ' '.join(str(attrs.get(key, '')) for key in ('class', 'alt', 'src')).lower()
        if any(word in marker for word in ('emoji', 'emoticon', '/face/', '/faces/', '/avatar/', '表情')):
            return
        self.urls.append(attrs.get('data-original') or attrs.get('data-src') or attrs.get('src'))


def extract_images(platform, item):
    urls = []
    # Outer post first, then any quoted original. Existing repost filtering remains upstream.
    for post in (item, item.get('retweeted_status')):
        if not isinstance(post, dict):
            continue
        if platform == 'weibo':
            pictures = post.get('pics', [])
            if isinstance(pictures, list):
                for picture in pictures:
                    if isinstance(picture, dict):
                        large = picture.get('large')
                        urls.append((large.get('url') if isinstance(large, dict) else None) or picture.get('url'))
            infos = post.get('pic_infos')
            if isinstance(infos, dict):
                ids = post.get('pic_ids') if isinstance(post.get('pic_ids'), list) else list(infos)
                for identifier in ids:
                    info = infos.get(str(identifier))
                    if isinstance(info, dict):
                        size = info.get('original') or info.get('large') or info.get('largest')
                        if isinstance(size, dict):
                            urls.append(size.get('url'))
        else:
            parser = ImageParser()
            parser.feed(str(post.get('text') or post.get('description') or ''))
            urls.extend(parser.urls)
    result = []
    for raw in urls:
        url = image_url(raw, platform)
        if url and url not in result:
            result.append(url)
        if len(result) >= MAX_IMAGES:
            break
    return result


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the already checked address, not a second DNS lookup (rebinding).
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def download_image(url, platform):
    safe = image_url(url, platform)
    if safe is None:
        raise ValueError('Invalid image URL')
    parts = urlsplit(safe)
    addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    ips = [entry[4][0] for entry in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('Image address is not public')
    connection = PinnedHTTPSConnection(parts.hostname, ips[0])
    deadline = time.monotonic() + 30
    try:
        path = parts.path + ('?' + parts.query if parts.query else '')
        # No cookies, authorization, environment proxy, redirects, or local file access.
        connection.request('GET', path, headers={'User-Agent': 'ShowAllOpts/1.0', 'Accept': 'image/*'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Image download rejected')
        length = response.getheader('Content-Length')
        if length and (not length.isdigit() or int(length) > MAX_BYTES):
            raise ValueError('Image exceeds size limit')
        mime = (response.getheader('Content-Type') or '').split(';')[0].strip().lower()
        if mime not in ('image/jpeg', 'image/png', 'image/webp'):
            raise ValueError('Unsupported image type')
        chunks, size = [], 0
        while size <= MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Image download timed out')
            if connection.sock is not None:
                connection.sock.settimeout(min(15, remaining))
            chunk = response.read1(min(64 * 1024, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b''.join(chunks)
        if not data or len(data) > MAX_BYTES:
            raise ValueError('Image exceeds size limit')
        valid = ((mime == 'image/jpeg' and data.startswith(b'\xff\xd8\xff'))
                 or (mime == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n'))
                 or (mime == 'image/webp' and data.startswith(b'RIFF') and data[8:12] == b'WEBP'))
        if not valid:
            raise ValueError('Image signature mismatch')
        return data, mime
    finally:
        connection.close()
