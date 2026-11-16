"""Regression cases for shared collectors; no JoinQuant or remote database access."""
import ast
import datetime as dt
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT/'collectors/策略批量采集_数据库版.py'


@pytest.fixture
def batch(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('common_collector', BATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROGRESS_ENABLED = False
    return module


def test_etf_paging_reads_all_capped_pages_and_preserves_filters(batch):
    base = declarative_base()
    class Source(base):
        __tablename__ = 'fund_data'
        id = Column(Integer, primary_key=True)
        code = Column(String)
    engine = create_engine('sqlite://')
    base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add_all([Source(id=i, code='keep' if i % 2 else 'exclude') for i in range(1, 18)])
        session.commit()
        counts = []
        def fetch(q):
            # Provider cap is smaller than the requested page, including a final short page.
            result = q.limit(2).all()
            counts.append(len(result))
            return pd.DataFrame([dict(id=r.id, code=r.code) for r in result], columns=['id', 'code'])
        ns = batch._etf_heat_strategy({'finance': SimpleNamespace(run_query=fetch), 'query': session.query}, batch.DB_CONFIG)
        frame = ns.read_pages(Source, (Source.code == 'keep',))
        assert frame['id'].tolist() == list(range(1, 18, 2))
        assert counts == [2, 2, 2, 2, 1, 0]
    engine.dispose()


@pytest.mark.parametrize('bad', [None, pd.DataFrame({'value':[3]}),
    pd.DataFrame({'id':[3,3]}), pd.DataFrame({'id':[4,3]}), pd.DataFrame({'id':[2]})])
def test_invalid_page_never_reaches_consumer(batch, bad):
    frames = iter([pd.DataFrame({'id':[1,2]}), bad])
    stream = batch._source_pages(lambda cursor: next(frames))
    assert next(stream)['id'].tolist() == [1,2]
    with pytest.raises(ValueError):
        next(stream)


def test_empty_page_required_fields_and_page_limit(batch):
    with pytest.raises(ValueError):
        list(batch._source_pages(lambda cursor: pd.DataFrame(), fields=('id',)))
    with pytest.raises(ValueError):
        list(batch._source_pages(lambda cursor: pd.DataFrame({'id':[1,2,3]}), page_size=2))
    assert list(batch._source_pages(lambda cursor: pd.DataFrame())) == []


def test_source_error_is_not_silently_treated_as_eof(batch):
    calls = []
    def fetch(cursor):
        calls.append(cursor)
        if cursor > 0:
            raise RuntimeError('source interrupted')
        return pd.DataFrame({'id':[1]})
    stream = batch._source_pages(fetch)
    assert not calls
    assert next(stream)['id'].tolist() == [1]
    with pytest.raises(RuntimeError, match='source interrupted'):
        next(stream)
    assert calls == [-1, 1]


def test_file_descriptor_exhaustion_retries_same_read_cursor_once(batch, monkeypatch):
    calls, recovered, messages = [], [], []
    monkeypatch.setattr(batch.gc, 'collect', lambda: recovered.append(True))
    monkeypatch.setattr(batch.os, 'listdir', lambda path: ['1', '2'])
    def fetch(cursor):
        calls.append(cursor)
        if len(calls) == 1:
            return pd.DataFrame({'id': [1]})
        if len(calls) == 2:
            raise OSError(24, 'Too many open files')
        if len(calls) == 3:
            return pd.DataFrame({'id': [2]})
        return pd.DataFrame({'id': []})
    rows = list(batch._source_pages(fetch, progress=messages.append))
    assert [i for frame in rows for i in frame['id']] == [1, 2]
    assert calls == [-1, 1, 1, 2] and recovered == [True]
    assert any('回收后 2 个' in message for message in messages)


@pytest.mark.parametrize('number,attempts', [(24, 2), (13, 1)])
def test_file_error_recovery_is_bounded_and_does_not_hide_other_errors(batch, monkeypatch, number, attempts):
    calls = []
    monkeypatch.setattr(batch.gc, 'collect', lambda: None)
    def fetch(cursor):
        calls.append(cursor)
        raise OSError(number, 'synthetic source file error')
    with pytest.raises(OSError) as failure:
        list(batch._source_pages(fetch))
    assert failure.value.errno == number and calls == [-1] * attempts


def test_page_retry_releases_objects_retained_by_failed_traceback(batch, monkeypatch):
    import weakref
    collect = batch.gc.collect
    refs, calls = [], []
    class Resource:
        pass
    def reclaim():
        assert sys.exc_info()[0] is None
        collect()
        assert refs[0]() is None
    def fetch(cursor):
        calls.append(cursor)
        if len(calls) == 1:
            resource = Resource()
            resource.cycle = resource
            refs.append(weakref.ref(resource))
            raise OSError(24, 'synthetic descriptor exhaustion')
        assert refs[0]() is None
        return pd.DataFrame({'id': []})
    monkeypatch.setattr(batch.gc, 'collect', reclaim)
    assert list(batch._source_pages(fetch)) == []
    assert calls == [-1, -1]


def test_numeric_policies_keep_decimal_precision_and_reject_bad_holdings(batch):
    micro = batch._microcap_strategy({}, batch.DB_CONFIG)
    dividend = batch._dividend_strategy({}, batch.DB_CONFIG)
    heat = batch._etf_heat_strategy({}, batch.DB_CONFIG)
    ownership = batch._ownership_strategy({}, batch.DB_CONFIG)
    assert micro.number('-0.123456789123456789') == Decimal('-0.123456789123456789')
    assert micro.rounded(Decimal('1.00000000005')) == Decimal('1.0000000000')
    assert dividend.rounded('1.2345675') == Decimal('1.234568')
    assert dividend.rounded('-1.2345675') == Decimal('-1.234568')
    assert dividend.number('Infinity') is None
    assert dividend.number('NaN') is None
    assert batch._round_decimal('1.234567895', 8) == Decimal('1.23456790')
    assert heat.positive('-1') is None
    assert ownership.number(None) is None
    with pytest.raises(ValueError):
        ownership.number(-1)


@pytest.mark.parametrize('hour,expected', [(15,22), (16,23), (20,23)])
def test_cutoff_retains_date_vs_announcement_separation(batch, hour, expected):
    now = dt.datetime(2026,10,23,hour,59)
    assert batch._closed_day(now.date(),now) == dt.date(2026,10,expected)
    historical = dt.date(2026,10,21)
    assert batch._closed_day(historical,now) == historical


def test_shared_helpers_match_standalone_files():
    main = {n.name:n for n in ast.parse(BATCH.read_text(encoding='utf-8'),feature_version=(3,6)).body
            if isinstance(n,ast.FunctionDef)}
    names = {'_source_pages','_decimal_number','_round_decimal','_optional_date','_closed_day','_checked_records',
             '_require_append', '_save_record_set', '_replace_latest_snapshot', '_checked_trade_days'}
    checked = set()
    for path in (ROOT/'collectors').glob('*数据库版.py'):
        if path == BATCH:
            continue
        for node in ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6)).body:
            if isinstance(node,ast.FunctionDef) and node.name in names:
                assert ast.dump(node) == ast.dump(main[node.name]), (path.name,node.name)
                checked.add(node.name)
    assert checked == names
