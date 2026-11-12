"""Atomic edits to the selected dotenv file without replacing its ownership or mode."""
from contextlib import contextmanager
from hashlib import sha256
from io import StringIO
import os
from pathlib import Path
import re
import stat
import tempfile

from dotenv import dotenv_values
from dotenv.parser import parse_stream
import portalocker

KEYS = {'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD',
        'ASHAREHUB_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'}

class EnvFileError(ValueError):
    pass

def selected_path(settings):
    value = getattr(settings, 'resolved_env_file', None)
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    from app.core.config import DEFAULT_ENV_FILE
    if 'APP_ENV_FILE' not in os.environ and path == DEFAULT_ENV_FILE.resolve() and not path.exists():
        return None
    return path

def _digest(raw):
    return sha256(raw).hexdigest()

def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError()
            raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise OSError()
        raw.decode('utf-8')
        return raw, info
    except (OSError, UnicodeError):
        raise EnvFileError('配置文件不存在、不可读或格式无效；未修改任何配置') from None

def _values(raw):
    try:
        text = raw.decode('utf-8')
        if any(item.error for item in parse_stream(StringIO(text))):
            raise ValueError()
        return dict(dotenv_values(stream=StringIO(text), interpolate=True))
    except Exception:
        raise EnvFileError('配置文件格式无效；未修改任何配置') from None

def render(raw, updates):
    if set(updates) - KEYS:
        raise EnvFileError('不允许修改此配置项')
    text = raw.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    remaining = set(updates)
    pieces = []
    for item in parse_stream(StringIO(text)):
        if item.error:
            raise EnvFileError('配置文件格式无效；未修改任何配置')
        if item.key not in updates:
            pieces.append(item.original.string)
            continue
        value = str(updates[item.key])
        if '${' in value or '\x00' in value:
            raise EnvFileError('配置值包含无法安全保存的变量插值或控制字符；未修改配置')
        quoted = "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
        original = item.original.string
        prefix = re.match(r'\s*', original).group(0)
        suffix = ''
        if original.count('\n') <= 1:
            quote = None
            escaped = False
            for index, character in enumerate(original):
                if escaped:
                    escaped = False
                    continue
                if character == '\\' and quote:
                    escaped = True
                    continue
                if character in ('"', "'"):
                    quote = None if quote == character else character if quote is None else quote
                if character == '#' and quote is None and index and original[index - 1].isspace():
                    suffix = ' ' + original[index:].rstrip('\r\n')
                    break
        pieces.append(prefix + item.key + '=' + quoted + suffix + newline)
        remaining.discard(item.key)
    result = ''.join(pieces)
    if remaining and result and not result.endswith(('\n', '\r')):
        result += newline
    for key in sorted(remaining):
        value = str(updates[key])
        if '${' in value or '\x00' in value:
            raise EnvFileError('配置值包含无法安全保存的变量插值或控制字符；未修改配置')
        result += key + "='" + value.replace('\\', '\\\\').replace("'", "\\'") + "'" + newline
    output = result.encode('utf-8')
    parsed = _values(output)
    if any(parsed.get(key) != str(value) for key, value in updates.items()):
        raise EnvFileError('配置无法无损保存；未修改任何配置')
    return output

def _atomic_replace(path, raw, info):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.app-config-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            current = os.fstat(stream.fileno())
            if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise EnvFileError('配置文件无法安全替换，请检查服务账号对配置文件及目录的权限；未扩大权限') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

class EnvTransaction:
    def __init__(self, settings):
        self.path = selected_path(settings)
        self.raw, self.info = _read(self.path) if self.path is not None else (None, None)
        self.file_values = _values(self.raw) if self.raw is not None else None
        self.values = dict(self.file_values) if self.file_values is not None else None
        self.override_values = {key.upper(): value for key, value in os.environ.items() if key.upper() in KEYS}
        self.override_names = {key.upper(): key for key in os.environ if key.upper() in KEYS}
        self.overridden = sorted(self.override_names.values())
        if self.values is not None:
            self.values.update(self.override_values)
        self.written = None

    def write(self, updates):
        if not updates:
            return
        if self.path is None:
            raise EnvFileError('未设置可写配置文件；未修改任何配置')
        overrides = sorted(self.override_names[key] for key in set(updates) & set(self.override_names))
        if overrides:
            raise EnvFileError('以下配置被环境变量覆盖，请先取消覆盖后再保存：' + '、'.join(overrides))
        current, current_info = _read(self.path)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns, value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode))
        if _digest(current) != _digest(self.raw) or identity(current_info) != identity(self.info):
            raise EnvFileError('配置文件已被其他操作修改，请刷新后重试')
        raw = render(self.raw, updates)
        self.written = raw
        _atomic_replace(self.path, raw, self.info)
        self.file_values = _values(raw)
        self.values = dict(self.file_values)
        self.values.update(self.override_values)

    def rollback(self):
        if self.written is None:
            return
        current, _ = _read(self.path)
        if current == self.raw:
            return
        if current != self.written:
            raise EnvFileError('保存期间配置文件发生外部变更；为保护新内容未覆盖文件，请刷新核验')
        _atomic_replace(self.path, self.raw, self.info)

@contextmanager
def env_transaction(settings):
    root = settings.log_path / 'system-configuration'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with portalocker.Lock(str(root / 'environment.lock'), timeout=10):
            transaction = EnvTransaction(settings)
            try:
                yield transaction
            except BaseException:
                transaction.rollback()
                raise
    except portalocker.exceptions.LockException:
        raise EnvFileError('配置文件正在更新，请稍后重试') from None
