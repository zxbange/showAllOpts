# -*- coding: utf-8 -*-
"""采集脚本统一 MySQL 配置；在聚宽研究目录中与采集脚本一起上传。

只在此文件配置数据库连接，修改后重新加载本模块并重新执行采集脚本。
真实连接信息不要提交 Git 或公开分享。本模块导入时不连接数据库。
"""
import builtins

DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_user',
    'dbpassword': 'your_password',
}

DB_OPTIONS = {
    'driver': 'auto',  # auto 优先 MySQLdb，其次 PyMySQL；也可填写 mysqldb 或 pymysql。
    'charset': 'utf8mb4',
    'connect_timeout': 10,
    'retry_count': 5,
    'retry_interval': 10,
    'pool_recycle': 3600,
    'pool_size': 1,
    'max_overflow': 0,
    'pool_pre_ping': True,
    'echo': False,
}


def get_database_config():
    return dict(DB_CONFIG)


def get_database_options():
    return dict(DB_OPTIONS)


def mysql_drivername(driver=None):
    driver = DB_OPTIONS['driver'] if driver is None else driver
    if driver not in ('auto', 'mysqldb', 'pymysql'):
        raise ValueError('driver 仅支持 auto、mysqldb 或 pymysql。')
    if driver == 'auto':
        try:
            import MySQLdb
            driver = 'mysqldb'
        except ImportError:
            import pymysql
            driver = 'pymysql'
    return 'mysql+' + driver


def engine_options(options=None):
    opts = DB_OPTIONS if options is None else options
    for name in ('connect_timeout', 'pool_recycle', 'retry_count', 'pool_size'):
        value = opts[name]
        if isinstance(value, builtins.bool) or not isinstance(value, builtins.int) or value <= 0:
            raise ValueError('MySQL 连接次数、超时及连接池大小须为正整数。')
    if opts['retry_interval'] < 0 or opts['max_overflow'] < 0:
        raise ValueError('MySQL 重试间隔及额外连接数不可为负。')
    return dict(echo=opts['echo'], pool_size=opts['pool_size'], max_overflow=opts['max_overflow'],
                pool_pre_ping=opts['pool_pre_ping'], pool_recycle=opts['pool_recycle'],
                connect_args={'connect_timeout': opts['connect_timeout']})


def create_mysql_engine():
    """项目命令行采集入口使用；不读取后端环境配置，也不主动建表或写数据。"""
    from sqlalchemy import create_engine
    from sqlalchemy.engine.url import URL
    config = get_database_config()
    opts = get_database_options()
    url = getattr(URL, 'create', URL)(drivername=mysql_drivername(opts['driver']),
        host=config['host'], port=builtins.int(config['port']), database=config['dbname'],
        username=config['dbuser'], password=config['dbpassword'], query={'charset': opts['charset']})
    engine = create_engine(url, **engine_options(opts))
    if hasattr(engine, 'hide_parameters'):
        engine.hide_parameters = True
    return engine
