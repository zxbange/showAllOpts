"""Versioned, checksummed JSON input snapshots. No pickle or executable payloads."""
import datetime
import decimal
import gzip
import hashlib
import io
import json
import math

import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, LargeBinary, String

RAW_VERSION = 'strategy_inputs_v1'
RAW_CHUNK_SIZE = 48 * 1024
RAW_TABLES = {
    '行业动量': ('industry-momentum', 'raw_industry_momentum'),
    '抱团拥挤度': ('market-crowding', 'raw_market_crowding'),
    '微盘温度计': ('microcap-temperature', 'raw_microcap_temperature'),
    '微盘新高新低': ('microcap-breadth', 'raw_microcap_breadth'),
    '高股息选股': ('dividend-selection', 'raw_dividend_selection'),
    '高增长': ('high-growth', 'raw_high_growth'),
    '净利润断层': ('earnings-gap', 'raw_earnings_gap'),
}


def raw_model(base, table):
    return type('Input_' + table, (base,), {
        '__tablename__': table,
        '__table_args__': {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
        'scope': Column(String(96), primary_key=True),
        'trade_date': Column(Date, primary_key=True),
        'part': Column(Integer, primary_key=True),
        'parts': Column(Integer, nullable=False),
        'digest': Column(String(64), nullable=False),
        'version': Column(String(32), nullable=False),
        'payload': Column(LargeBinary, nullable=False),
        'updated_at': Column(DateTime, nullable=False),
    })


def pack_input(value):
    if isinstance(value, pd.DataFrame):
        return {'__kind__': 'frame', 'columns': pack_input(list(value.columns)),
                'index': pack_input(list(value.index)), 'data': pack_input(value.values.tolist())}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value) and '__kind__' not in value:
            return {k: pack_input(v) for k, v in value.items()}
        return {'__kind__': 'mapping', 'items': [[pack_input(k), pack_input(v)] for k,v in value.items()]}
    if isinstance(value, (set, tuple)):
        items = sorted(value) if isinstance(value, set) else value
        return {'__kind__': 'tuple', 'items': [pack_input(v) for v in items]}
    if isinstance(value, list):
        return [pack_input(v) for v in value]
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, decimal.Decimal):
        return {'__kind__': 'decimal', 'value': str(value)} if value.is_finite() else None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return {'__kind__': 'datetime' if isinstance(value, datetime.datetime) else 'date', 'value': value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'item'):
        return pack_input(value.item())
    raise ValueError('Unsupported raw input type: ' + type(value).__name__)


def unpack_input(value):
    if isinstance(value, list):
        return [unpack_input(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get('__kind__')
    if kind == 'date':
        return datetime.datetime.strptime(value['value'], '%Y-%m-%d').date()
    if kind == 'datetime':
        return pd.Timestamp(value['value']).to_pydatetime()
    if kind == 'decimal':
        return decimal.Decimal(value['value'])
    if kind == 'tuple':
        return tuple(unpack_input(v) for v in value['items'])
    if kind == 'mapping':
        return {unpack_input(k): unpack_input(v) for k,v in value['items']}
    if kind == 'frame':
        return pd.DataFrame(unpack_input(value['data']), columns=unpack_input(value['columns']), index=unpack_input(value['index']))
    if kind:
        raise ValueError('Unknown raw input encoding')
    return {k:unpack_input(v) for k,v in value.items()}


def encode_input(data):
    raw = json.dumps(pack_input(data), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0) as stream:
        stream.write(raw)
    payload = buffer.getvalue()
    return hashlib.sha256(payload).hexdigest(), [payload[i:i+RAW_CHUNK_SIZE] for i in range(0,len(payload),RAW_CHUNK_SIZE)]


def decode_input(rows):
    rows = sorted(rows, key=lambda r:r.part)
    if not rows or [r.part for r in rows] != list(range(rows[0].parts)):
        raise ValueError('原始数据分片不完整，不能发布计算结果')
    first = rows[0]
    if any((r.version,r.scope,r.trade_date,r.parts,r.digest) != (RAW_VERSION,first.scope,first.trade_date,first.parts,first.digest) for r in rows):
        raise ValueError('原始数据版本或分片不一致')
    payload = b''.join(r.payload for r in rows)
    if hashlib.sha256(payload).hexdigest() != first.digest:
        raise ValueError('原始数据校验失败')
    return unpack_input(json.loads(gzip.decompress(payload).decode('utf-8')))
