"""Optional MySQL JSON wire compression; SQLite stores ordinary JSON text."""
import logging
import struct
import zlib

from sqlalchemy import LargeBinary, cast, func, select
from sqlalchemy.dialects.mysql import CHAR
from sqlalchemy.exc import DBAPIError

from app.core.cache_errors import SnapshotDataError

logger = logging.getLogger(__name__)
OPTION = 'ownership_json_compression'


def configure_compression(source, enabled):
    supported = False
    if enabled and source.dialect.name == 'mysql':
        try:
            packet = source.scalar(select(func.compress('ownership-sync', type_=LargeBinary)))
            supported = packet is not None and decompress_json(packet) == 'ownership-sync'
        except DBAPIError as exc:
            if getattr(exc.orig, 'args', (None,))[0] != 1305:
                raise  # A disconnected connection must be retried, not reused.
        logger.info('Cache ownership JSON compression enabled=%s', supported)
    source.execution_options(**{OPTION: supported})


def compressed_column(column):
    return func.compress(cast(column, CHAR(charset='utf8mb4')), type_=LargeBinary).label(column.name)


def decompress_json(packet):
    if packet is None:
        return None
    try:
        packet = bytes(packet)
        if not packet:
            return ''  # MySQL COMPRESS('') is the empty binary string.
        expected = struct.unpack('<I', packet[:4])[0]
        data = zlib.decompress(packet[4:])
        if len(data) != expected:
            raise ValueError('length mismatch')
        return data.decode('utf-8')
    except (ValueError, TypeError, struct.error, zlib.error, UnicodeError):
        raise SnapshotDataError('机构持仓压缩数据校验失败，保留旧缓存，请检查源库或关闭压缩后重试。') from None


def decode_rows(batch, omitted, compressed):
    values = []
    for row in batch:
        record = dict(row._mapping, **{name: '' for name in omitted})
        for name in compressed:
            record[name] = decompress_json(record[name])
        values.append(record)
    return values
