"""Safe cache failure diagnostics: codes and static hints, never SQL or credentials."""
import errno
from pathlib import Path
import sqlite3
import traceback

from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout


def failure_details(exc):
    original = getattr(exc, 'orig', exc)
    code = 'UNKNOWN'
    hint = '请检查应用错误日志中的失败位置、数据库连接及表结构。'
    if isinstance(original, sqlite3.Error):
        code = getattr(original, 'sqlite_errorname', 'SQLITE_ERROR')
        number = getattr(original, 'sqlite_errorcode', 0) & 255
        hint = {
            sqlite3.SQLITE_BUSY: '本地缓存文件被占用，请检查是否有其他进程正在修改缓存。',
            sqlite3.SQLITE_LOCKED: '本地缓存文件被锁定，请检查缓存目录和其他进程。',
            sqlite3.SQLITE_READONLY: '本地缓存目录不可写，请检查运行账号及目录权限。',
            sqlite3.SQLITE_FULL: '本地缓存所在磁盘空间不足，请检查磁盘剩余空间。',
            sqlite3.SQLITE_CANTOPEN: '无法打开本地缓存文件，请检查路径、运行账号及目录权限。',
            sqlite3.SQLITE_CORRUPT: '本地缓存损坏，可对该模块执行 --full 重建。',
            sqlite3.SQLITE_NOTADB: '本地缓存不是有效 SQLite 文件，可对该模块执行 --full 重建。',
            sqlite3.SQLITE_CONSTRAINT: '本地缓存写入违反数据约束，请检查源数据是否存在空值或重复主键。',
        }.get(number, '本地 SQLite 操作失败，请检查数据库版本及缓存文件。')
        # Match fixed error patterns internally; never return the driver message itself.
        if 'too many sql variables' in str(original).lower():
            hint = '本地 SQLite 参数数量超过限制，请检查 SQLite 版本和同步批次大小。'
    elif isinstance(original, OSError):
        code = 'OS_' + str(original.errno or 'UNKNOWN')
        hint = {
            errno.EACCES: '本地缓存目录权限不足，请检查服务运行账号及缓存文件所有者。',
            errno.EPERM: '本地缓存操作被系统拒绝，请检查服务目录权限和安全策略。',
            errno.ENOSPC: '本地缓存所在磁盘空间不足，请检查磁盘剩余空间。',
            errno.EROFS: '本地缓存目录位于只读文件系统，请检查挂载与服务目录权限。',
            errno.ENOENT: '本地缓存文件或目录不存在，请检查是否被其他程序移走。',
        }.get(original.errno, '本地缓存文件操作失败，请检查目录权限、磁盘和挂载状态。')
    elif isinstance(exc, PoolTimeout):
        code, hint = 'POOL_TIMEOUT', '等待数据库连接超时，请检查已有任务和数据库连接池。'
    else:
        args = getattr(original, 'args', ())
        if isinstance(exc, DBAPIError) and args and isinstance(args[0], int):
            code = 'MYSQL_' + str(args[0])
            hint = {
                1044: '源数据库访问权限不足，请检查数据库账号授权。',
                1045: '源数据库认证失败，请检查服务使用的配置和数据库授权。',
                1054: '源表缺少程序需要的字段，请核对采集器与项目的表结构。',
                1142: '源表读取权限不足，请检查数据库账号的 SELECT 权限。',
                1146: '源数据库中找不到需要的数据表，请检查数据库配置和采集入库情况。',
                2003: '无法连接源数据库，请检查 Linux 到数据库的网络和访问规则。',
                2006: '源数据库连接已断开，请检查网络和数据库连接超时设置。',
                2013: '读取源数据库时连接中断或超时，请检查公网链路与查询耗时。',
            }.get(args[0], '源数据库查询失败，请依据错误码检查数据库及表结构。')
    frames = ' > '.join('{}:{}:{}'.format(Path(f.filename).name, f.lineno, f.name)
                        for f in traceback.extract_tb(exc.__traceback__))
    return code, hint, frames or '-'
