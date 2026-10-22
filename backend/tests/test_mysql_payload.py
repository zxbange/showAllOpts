import struct
import zlib
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, JSON, MetaData, Table, select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

from app.core.cache_errors import SnapshotDataError
from app.core.mysql_payload import OPTION, compressed_column, configure_compression, decompress_json


def packet(value):
    data=value.encode('utf-8')
    return struct.pack('<I',len(data))+zlib.compress(data)


@pytest.mark.parametrize('value', ['null','{}','[]','"中文😀"','{"x":12345678901234567890,"y":0.1234567890123456789}', 'trailing space '])
def test_mysql_packet_preserves_utf8_and_numbers(value):
    assert decompress_json(packet(value)) == value
    assert decompress_json(packet(value)+b'.') == value


@pytest.mark.parametrize('value', [b'abc', struct.pack('<I',999)+zlib.compress(b'{}'), b'private_bad_packet'])
def test_corruption_fails_without_exposing_data(value):
    with pytest.raises(SnapshotDataError) as exc:
        decompress_json(value)
    assert 'private_bad_packet' not in str(exc.value)


def test_null_and_empty_packets():
    assert decompress_json(None) is None
    assert decompress_json(b'') == ''


class Source:
    dialect=SimpleNamespace(name='mysql')
    def __init__(self, value):
        self.value=value
        self.options={}
        self.calls=0
    def scalar(self, query):
        self.calls+=1
        if isinstance(self.value, Exception): raise self.value
        return self.value
    def execution_options(self, **values): self.options.update(values)


def test_capability_fallback_and_opt_out():
    source=Source(None)
    configure_compression(source,True)
    assert source.options[OPTION] is False
    source=Source(packet('ownership-sync'))
    configure_compression(source,True)
    assert source.options[OPTION] is True
    configure_compression(source,False)
    assert source.options[OPTION] is False and source.calls == 1


def test_network_failure_not_hidden_as_missing_compression():
    from pymysql.err import OperationalError as DriverError
    source=Source(OperationalError('private',{},DriverError(2013,'disconnected')))
    with pytest.raises(OperationalError): configure_compression(source,True)
    source=Source(OperationalError('private',{},DriverError(1305,'missing function')))
    configure_compression(source,True)
    assert source.options[OPTION] is False


def test_mysql_query_uses_binary_compression_and_explicit_utf8():
    table=Table('report',MetaData(),Column('payload',JSON))
    sql=str(select(compressed_column(table.c.payload)).compile(dialect=mysql.dialect()))
    assert 'compress(CAST(report.payload AS CHAR CHARACTER SET utf8mb4))' in sql
