"""JSON cache copies preserve representation and bypass object conversion."""
import logging

from sqlalchemy import Column, Integer, JSON, MetaData, Table, Text, create_engine, event, select, type_coerce
from sqlalchemy.dialects import mysql

from app.core import cache_sync


def test_json_transfer_bypasses_codecs_preserves_values_and_closes_read(tmp_path, monkeypatch, caplog):
    metadata = MetaData()
    table = Table('sample_json', metadata, Column('id', Integer, primary_key=True), Column('payload', JSON))
    source = create_engine('sqlite:///' + (tmp_path/'source.sqlite').as_posix())
    target = create_engine('sqlite:///' + (tmp_path/'target.sqlite').as_posix())
    metadata.create_all(source)
    metadata.create_all(target)
    documents = [
        '{ "name": "社保基金😀", "items": [null, true, false, 12345678901234567890, 0.1234567890123456789] }',
        'null', None, '[]', '"string value"', '{"a":{"b":[1,2,3]}}',
    ]
    with source.begin() as connection:
        for i, value in enumerate(documents):
            connection.exec_driver_sql('INSERT INTO sample_json VALUES (?, ?)', (i, value))
    source.dispose()
    target.dispose()
    def forbidden(*args):
        raise AssertionError('JSON conversion must not run during a text copy')
    source = create_engine(source.url, json_deserializer=forbidden)
    target = create_engine(target.url, json_serializer=forbidden)
    monkeypatch.setattr(cache_sync, 'READ_PAGE_SIZE', 2)
    results = []
    def read_result(conn, clause, multiparams, params, options, result):
        results.append(result)
    def start_write(conn, cursor, sql, *args):
        if sql.lstrip().startswith('INSERT'):
            assert results and all(result.closed for result in results)
    event.listen(source, 'after_execute', read_result)
    event.listen(target, 'before_cursor_execute', start_write)
    with caplog.at_level(logging.INFO):
        with source.connect() as src, target.begin() as dst:
            assert cache_sync.transfer(src, dst, table) == len(documents)
    with target.connect() as connection:
        actual = connection.scalars(select(type_coerce(table.c.payload, Text)).order_by(table.c.id)).all()
        assert actual == documents  # Not double-encoded, SQL NULL distinct from JSON null.
        decoded = connection.execute(select(table).order_by(table.c.id)).all()
        assert decoded[0].payload['name'] == '社保基金😀'
        assert decoded[1].payload is None and decoded[2].payload is None
    assert 'read_seconds=' in caplog.text and 'write_seconds=' in caplog.text
    assert 'raw_json=True' in caplog.text
    assert documents[0] not in caplog.text
    source.dispose()
    target.dispose()


def test_mysql_text_coercion_does_not_add_server_side_json_conversion():
    table = Table('report', MetaData(), Column('id', Integer, primary_key=True), Column('payload', JSON))
    statement = select(table.c.id, type_coerce(table.c.payload, Text).label('payload'))
    sql = str(statement.compile(dialect=mysql.dialect()))
    assert 'CAST' not in sql and 'JSON_EXTRACT' not in sql
    assert 'report.payload AS payload' in sql
