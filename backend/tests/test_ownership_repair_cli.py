import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]


def load_cli():
    spec = importlib.util.spec_from_file_location('repair_ownership_cli', ROOT / 'scripts' / 'repair_ownership_classification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repair_db(tmp_path, monkeypatch):
    cli = load_cli()
    collector = cli.load_collector()
    collector.china_now = lambda: dt.datetime(2026, 10, 26, 15)
    ns = collector._ownership_strategy({'finance': None, 'query': None}, collector.DB_CONFIG)
    engine = create_engine('sqlite:///' + str(tmp_path / 'repair.sqlite'))
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(ns.OwnershipStock(code='002281.XSHE', name='光迅科技', updated_at=dt.datetime(2026, 10, 26)))
        for period, known, shares in [(dt.date(2026, 6, 30), True, 100), (dt.date(2026, 9, 30), False, 200)]:
            members = [dict(id=str(rank) if known else None, name='公司' + str(rank), rank=rank,
                            class_id=307099 if known else None, shares=100, ratio=1, market_value=1000)
                       for rank in range(1, 11)]
            members[6].update(name='全国社保基金五零二组合', class_id=307004 if known else None,
                              shares=shares, market_value=shares * 10)
            payload = dict(categories={'public_fund': None, 'social': None}, float_members=members,
                           float_complete=known, social_entry={'status': 'unknown'})
            session.add(ns.OwnershipReport(code='002281.XSHE', report_date=period, payload=payload,
                                           updated_at=dt.datetime(2026, 10, 26)))
        session.add(ns.DisclosureProbe(key='main', as_of=dt.date(2026, 10, 26), payload={}))
        session.add(ns.SyncHead(id='main', generation='test', revision=5))
        session.commit()
    monkeypatch.setattr(cli, 'Settings', lambda: SimpleNamespace())
    monkeypatch.setattr(cli, 'build_engine', lambda settings, **kw: engine)
    monkeypatch.setattr(cli, 'load_collector', lambda: collector)
    yield cli, collector, ns, engine, Session
    engine.dispose()


def test_default_preview_never_writes_or_advances_journal(repair_db, capsys):
    cli, _, ns, engine, Session = repair_db
    writes = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, sql, *args:
                 writes.append(sql) if sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP') else None)
    assert cli.main(['--code', '002281']) == 0
    assert not writes
    assert '只读预览' in capsys.readouterr().out
    with Session() as session:
        assert session.query(ns.SyncHead).one().revision == 5
        current = session.query(ns.OwnershipReport).filter_by(report_date=dt.date(2026, 9, 30)).one()
        assert current.payload['categories']['social'] is None


def test_apply_records_changes_and_rerun_is_idempotent(repair_db, capsys):
    cli, _, ns, _, Session = repair_db
    assert cli.main(['--code', '002281.XSHE', '--apply']) == 0
    assert '已修复' in capsys.readouterr().out
    with Session() as session:
        current = session.query(ns.OwnershipReport).filter_by(report_date=dt.date(2026, 9, 30)).one()
        assert current.payload['categories']['social']['shares'] == 200
        assert current.payload['social_complete'] is True
        revision = session.query(ns.SyncHead).one().revision
        assert revision > 5
        assert session.query(ns.Change).filter_by(code='002281.XSHE', record_date=dt.date(2026, 9, 30)).one().revision == revision
    assert cli.main(['--code', '002281.XSHE', '--apply']) == 0
    assert '0 份报告' in capsys.readouterr().out
    with Session() as session:
        assert session.query(ns.SyncHead).one().revision == revision


def test_future_collection_date_rejected_without_changes(repair_db, capsys):
    cli, _, ns, _, Session = repair_db
    with Session() as session:
        session.query(ns.DisclosureProbe).filter_by(key='main').one().as_of = dt.date(2026, 10, 27)
        session.commit()
    assert cli.main(['--apply']) == 2
    assert '修复日期早于' in capsys.readouterr().out
    with Session() as session:
        assert session.query(ns.SyncHead).one().revision == 5
        assert session.query(ns.Change).count() == 0


def test_missing_tables_not_created_and_errors_are_redacted(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    engine = create_engine('sqlite:///' + str(tmp_path / 'empty.sqlite'))
    monkeypatch.setattr(cli, 'Settings', lambda: SimpleNamespace())
    monkeypatch.setattr(cli, 'build_engine', lambda *args, **kw: engine)
    assert cli.main([]) == 2
    assert cli.inspect(engine).get_table_names() == []
    monkeypatch.setattr(cli, 'build_engine', lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('mysql://user:SECRET@host/db')))
    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert 'RuntimeError' in output and 'SECRET' not in output and 'mysql://' not in output


@pytest.mark.parametrize('existing', [False, True])
def test_loading_collector_restores_jqdata(existing, monkeypatch):
    cli = load_cli()
    sentinel = ModuleType('real_jqdata')
    if existing:
        monkeypatch.setitem(sys.modules, 'jqdata', sentinel)
    else:
        monkeypatch.delitem(sys.modules, 'jqdata', raising=False)
    module = cli.load_collector()
    assert callable(module._ownership_strategy)
    assert sys.modules.get('jqdata') is sentinel if existing else 'jqdata' not in sys.modules


def test_rejects_invalid_or_ambiguous_stock_codes(repair_db, capsys):
    cli, _, _, _, _ = repair_db
    with pytest.raises(SystemExit):
        cli.main(['--code', '002281;DROP'])
    assert cli.main(['--code', '999999']) == 2
    assert '未找到唯一已有档案' in capsys.readouterr().out