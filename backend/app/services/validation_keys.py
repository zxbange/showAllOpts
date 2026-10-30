"""Private persistent encryption key, separate from the management SQLite database."""
import os
from pathlib import Path
import stat
import tempfile

from cryptography.fernet import Fernet
import portalocker


POSIX_KEY_STORAGE = os.name == 'posix'


class KeyStorageError(ValueError):
    pass


def persistent_cipher(root: Path, configured: str, validate):
    """Validate before publication; never replace existing material after a failure.

    validate(cipher) must reject a key that cannot decrypt existing source records.
    validate(None) must reject generation when encrypted data already exists.
    """
    if not POSIX_KEY_STORAGE:
        # Preserve existing Windows deployments using an external encryption key.
        # Never pretend chmod supplies a Windows ACL or save an unprotected key.
        if not configured:
            raise KeyStorageError('此系统尚不支持自动密钥安全存储；自动初始化仅支持Linux部署，已有外部加密密钥仍可使用')
        try:
            cipher = Fernet(configured.encode('ascii'))
            validate(cipher)
            return cipher
        except KeyStorageError:
            raise
        except Exception:
            raise KeyStorageError('已有验证加密密钥无效，已停止操作') from None
    directory = root.parent / 'short-validation-secrets'
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise KeyStorageError('验证密钥目录权限异常，已停止操作，请由站主管理员恢复受限权限')
        lock_path = directory / 'initialization.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as lock:
            portalocker.lock(lock, portalocker.LOCK_EX)
            path = directory / 'source.key'
            stored = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                pass
            else:
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                        raise KeyStorageError('验证密钥文件权限异常，已停止操作')
                    stored = stream.read(1024)
            if configured:
                value = configured.encode('ascii')
                if stored is not None and stored != value:
                    raise KeyStorageError('已有验证密钥与旧配置不一致，已停止操作；不会覆盖密钥')
            elif stored is not None:
                value = stored
            else:
                validate(None)
                value = Fernet.generate_key()
            cipher = Fernet(value)
            validate(cipher)
            if stored is None:
                fd, temporary = tempfile.mkstemp(prefix='.source-', dir=directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(value)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return cipher
    except KeyStorageError:
        raise
    except Exception:
        raise KeyStorageError('验证密钥初始化或读取失败，已停止操作；请保留原密钥和管理数据库以便恢复') from None
