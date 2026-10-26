"""Source-classification gaps must not erase holdings or invent comparable reports."""
import copy
import ast
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import Column, Date, Float, Integer, String, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.services.ownership import social_increase
from test_ownership_collector import SOURCE, mod, ns, member


JUNE = dt.date(2026, 6, 30)
SEPTEMBER = dt.date(2026, 9, 30)
AS_OF = dt.date(2026, 10, 26)
STAMP = dt.datetime(2026, 10, 20)
CODE = '002281.XSHE'


def source_rows(period):
    """The user's two actual shareholder lists, retaining the original missing classes."""
    common = [
        ('烽火科技集团有限公司', 307099, 291478944),
        ('国新投资有限公司', 307099, 30999271),
        ('香港中央结算有限公司', 307099, 18453745),
        ('中国信息通信科技集团有限公司', 307099, 16960646),
        ('中国工商银行股份有限公司-国泰中证全指通信设备交易型开放式指数证券投资基金', 307003, 9131176),
    ]
    if period == JUNE:
        rows = common + [
            ('卜浩文', 307001, 3842300),
            ('中国工商银行股份有限公司-易方达中证人工智能主题交易型开放式指数证券投资基金', 307003, 3044721),
            ('中国银行股份有限公司-华夏中证5G通信主题交易型开放式指数证券投资基金', 307003, 2205781),
            ('全国社保基金五零二组合', 307004, 1872908),
            ('中国建设银行股份有限公司-景顺长城优质成长股票型证券投资基金', 307003, 1634101),
        ]
        pub = dt.date(2026, 8, 20)
    else:
        rows = [(name, kind, shares) for name, kind, shares in common]
        rows[1] = (rows[1][0], 307099, 33843694)
        rows[2] = (rows[2][0], 307099, 17436665)
        rows[4] = (rows[4][0], 307003, 9295125)
        rows += [
            ('交通银行股份有限公司-德邦鑫星价值灵活配置混合型证券投资基金', 307003, 3487700),
            ('全国社保基金五零二组合', 307004, 3348837),
            ('中国工商银行股份有限公司-易方达中证人工智能主题交易型开放式指数证券投资基金', 307003, 3258748),
            ('中国建设银行股份有限公司-前海开源沪港深乐享生活灵活配置混合型证券投资基金', 307003, 3231000),
            ('中信银行股份有限公司-中欧信息科技混合型发起式证券投资基金', 307003, 3072037),
        ]
        pub = AS_OF
    return [dict(id=(8508160 if period == JUNE else 8634144) + i,
                 shareholder_rank=i + 1, pub_date=pub, share_number=shares,
                 shareholder_id=str(i + 1) if period == JUNE else None,
                 shareholder_name=name, shareholder_class_id=kind if period == JUNE else None,
                 share_ratio=(0.2263 if period == JUNE else 0.4045) if '社保' in name else 1)
            for i, (name, kind, shares) in enumerate(rows)]


def stored_payload(ns, period):
    members, complete, pub = ns.float_snapshot(source_rows(period), 2)
    categories = {'public_fund': {'shares': 759000, 'market_value': 125143920,
                                 'holder_count': 3, 'scope': 'fund_disclosed', 'value_method': 'reported'}}
    for category, ids in ns.CATEGORY_IDS.items():
        categories[category] = ns.aggregate([m for m in members if m['class_id'] in ids], 'top10_float', 'estimated')
    payload = dict(schema_version=1, float_members=members, float_complete=complete,
                   float_pub_date=pub, categories=categories)
    payload['social_entry'] = ns.social_entry(payload, None)
    return payload


def test_guangxun_missing_source_classes_recover_from_names_and_prior_source(ns):
    previous = stored_payload(ns, JUNE)
    current = stored_payload(ns, SEPTEMBER)
    original = copy.deepcopy(current)
    result = ns.classify_payload(current, [previous])
    assert current == original  # Classification must not mutate stored/source payloads.
    assert result['float_list_complete'] and result['float_complete'] and result['social_complete']
    assert result['categories']['public_fund'] == current['categories']['public_fund']
    social = next(m for m in result['float_members'] if m['name'] == '全国社保基金五零二组合')
    assert social['class_id'] == 307004 and social['raw_class_id'] is None
    assert social['class_source'] == 'history' and social['is_social'] is True
    assert result['categories']['social']['shares'] == 3348837
    assert result['categories']['social']['complete'] is True
    assert sum(m.get('class_source') == 'name' for m in result['float_members']) == 3
    entry = ns.social_entry(result, previous)
    assert entry['status'] == 'comparable'
    assert not entry['first_appearance'] and not entry['new_combinations']
    increase = social_increase(result, previous)
    assert increase['status'] == 'comparable'
    assert increase['previous_shares'] == 1872908
    assert increase['current_shares'] == 3348837 and increase['change_shares'] == 1475929
    assert increase['current_ratio'] == 0.4045


def test_unresolved_other_holder_preserves_known_social_but_blocks_comparison(ns):
    previous = stored_payload(ns, JUNE)
    current = stored_payload(ns, SEPTEMBER)
    current['float_members'][0]['name'] = '待确认账户'
    result = ns.classify_payload(current, [previous])
    assert result['float_list_complete'] is True
    assert result['float_complete'] is False and result['social_complete'] is False
    assert result['categories']['social']['shares'] == 3348837
    assert result['categories']['social']['complete'] is False
    assert ns.social_entry(result, previous)['status'] == 'unknown'
    assert social_increase(result, previous)['status'] == 'unknown'


@pytest.mark.parametrize('damage', ['missing_row', 'missing_name', 'duplicate_rank'])
def test_incomplete_or_ambiguous_list_cannot_be_comparable(ns, damage):
    previous = stored_payload(ns, JUNE)
    current = stored_payload(ns, SEPTEMBER)
    if damage == 'missing_row':
        current['float_members'].pop()
    elif damage == 'missing_name':
        current['float_members'][0]['name'] = '  '
    else:
        current['float_members'][0]['rank'] = current['float_members'][1]['rank']
    result = ns.classify_payload(current, [previous])
    assert result['float_list_complete'] is False and result['social_complete'] is False
    assert ns.social_entry(result, previous)['status'] == 'unknown'


@pytest.mark.parametrize('name', [
    '基本养老保险基金一零一组合',
    '全国社保基金投资管理有限公司',
    '社保基金管理账户',
    '某机构全国社保基金五零二组合',
    '中国银行股份有限公司',
    '中国银行股份有限公司-未知账户',
    '某私募股票型证券投资基金',
    '某养老混合型证券投资基金',
])
def test_broad_keywords_never_guess_a_missing_class(ns, name):
    result = ns.classify_member(member(identity=None, name=name, kind=None), [])
    assert result['class_id'] is None and result['is_social'] is None
    assert result['class_source'] == 'unknown'


@pytest.mark.parametrize('name', [
    '全国社保基金五零二组合',
    '全国社保基金502组合',
    '全国社会保障基金五零二组合',
    '广发基金管理有限公司－社保基金四三零组合',
    ' 某基金管理有限公司 — 全国社保基金五零二组合 ',
])
def test_explicit_social_product_names_are_supported(ns, name):
    result = ns.classify_member(member(identity=None, name=name, kind=None), [])
    assert result['class_id'] == 307004 and result['is_social'] is True
    assert result['class_source'] == 'name' and result['raw_class_id'] is None


def test_current_source_class_wins_and_conflicting_history_is_not_guessed(ns):
    name = '全国社保基金五零二组合'
    authoritative = member(name=name, kind=307099)
    result = ns.classify_member(authoritative, [{'float_members': [member(name=name)]}])
    assert result['class_id'] == 307099
    assert ns.social_flag(result) is False
    missing = member(identity=None, name=name, kind=None)
    for history in ([member(name=name, kind=307099)],
                    [member(name=name, kind=307099), member(name=name, kind=307004)]):
        result = ns.classify_member(missing, [{'float_members': history}])
        assert result['class_id'] is None and result['is_social'] is None
        assert result['class_source'] == 'conflict'


def test_history_only_uses_original_classification_not_prior_inference(ns):
    name = '无法凭名称确定的机构'
    missing = member(identity=None, name=name, kind=None)
    inferred = dict(member(identity=None, name=name, kind=307004), raw_class_id=None,
                    class_source='history', is_social=True)
    result = ns.classify_member(missing, [{'float_members': [inferred]}])
    assert result['class_id'] is None and result['class_source'] == 'unknown'
    original = member(identity=None, name=name, kind=307004)
    result = ns.classify_member(missing, [{'float_members': [original]}])
    assert result['class_id'] == 307004 and result['class_source'] == 'history'


@pytest.fixture
def database(ns, tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'classification.sqlite'))
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    try:
        yield engine, Session
    finally:
        engine.dispose()


def seed_reports(ns, Session, codes=(CODE,)):
    with Session() as session:
        for code in codes:
            session.add(ns.OwnershipStock(code=code, name='测试股票', updated_at=STAMP))
            for period in (JUNE, SEPTEMBER):
                payload = stored_payload(ns, period)
                session.add(ns.OwnershipReport(code=code, report_date=period, payload=payload, updated_at=STAMP))
        session.commit()


def test_repair_dry_run_journal_idempotency_and_preserved_public_funds(ns, database):
    engine, Session = database
    seed_reports(ns, Session)
    ns.install_change_tracking(Session)
    with Session() as session:
        before = [(r.report_date, copy.deepcopy(r.payload), r.updated_at)
                  for r in session.query(ns.OwnershipReport).order_by(ns.OwnershipReport.report_date)]
        revision = session.query(ns.SyncHead).one().revision
    writes = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(' ', 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    count = ns.repair_classifications(Session, AS_OF, dry_run=True)
    assert count > 0 and writes == []
    event.remove(engine, 'before_cursor_execute', capture)
    with Session() as session:
        assert session.query(ns.DisclosureProbe).filter_by(key='classify-v1').first() is None
        assert session.query(ns.SyncHead).one().revision == revision
        assert session.query(ns.Change).count() == 0
        assert [(r.report_date, r.payload, r.updated_at)
                for r in session.query(ns.OwnershipReport).order_by(ns.OwnershipReport.report_date)] == before
    changed = set()
    assert ns.repair_classifications(Session, AS_OF, changed=changed) == count
    with Session() as session:
        repaired = session.query(ns.OwnershipReport).filter_by(code=CODE, report_date=SEPTEMBER).one()
        assert repaired.payload['categories']['social']['shares'] == 3348837
        assert repaired.payload['categories']['public_fund'] == before[1][1]['categories']['public_fund']
        assert repaired.payload['social_entry']['status'] == 'comparable'
        assert not repaired.payload['social_entry']['new_combinations']
        assert session.query(ns.DisclosureProbe).filter_by(key='classify-v1').one().payload['complete']
        journal = session.query(ns.Change).filter_by(code=CODE, table_name='ownership_report').all()
        assert {r.record_date for r in journal} == {key[2] for key in changed}
        assert all(r.deleted == 0 for r in journal)
        after_revision = session.query(ns.SyncHead).one().revision
        assert after_revision > revision
    assert ns.repair_classifications(Session, AS_OF) == 0
    assert ns.repair_classifications(Session, AS_OF, codes=[CODE]) == 0
    with Session() as session:
        assert session.query(ns.SyncHead).one().revision == after_revision


def test_repair_uses_only_past_source_evidence(ns, database):
    _, Session = database
    unknown = stored_payload(ns, SEPTEMBER)
    known = stored_payload(ns, JUNE)
    # A future report cannot supply an ID to an older unknown account.
    unknown['float_members'][0]['name'] = '仅未来报告识别的机构'
    future = copy.deepcopy(known)
    future['float_members'][0]['name'] = '仅未来报告识别的机构'
    with Session() as session:
        session.add_all([
            ns.OwnershipReport(code=CODE, report_date=JUNE, payload=unknown, updated_at=STAMP),
            ns.OwnershipReport(code=CODE, report_date=SEPTEMBER, payload=future, updated_at=STAMP),
        ])
        session.commit()
        updates = ns.classification_updates(session, [CODE], AS_OF)
        lookup = {r.report_date: p for r, p in updates}
        assert lookup[JUNE]['float_members'][0]['class_id'] is None
        assert lookup.get(SEPTEMBER, future)['social_entry']['status'] == 'unknown'


def test_repair_previous_quarter_recalculates_already_stored_following_comparison(ns, database):
    _, Session = database
    first = stored_payload(ns, JUNE)
    missing = stored_payload(ns, SEPTEMBER)
    following = copy.deepcopy(missing)
    for m in following['float_members']:
        m['class_id'] = 307004 if '社保' in m['name'] else 307003 if '证券投资基金' in m['name'] else 307099
    following['float_complete'] = True
    with Session() as session:
        session.add_all([
            ns.OwnershipReport(code=CODE, report_date=dt.date(2026, 3, 31), payload=first, updated_at=STAMP),
            ns.OwnershipReport(code=CODE, report_date=JUNE, payload=missing, updated_at=STAMP),
            ns.OwnershipReport(code=CODE, report_date=SEPTEMBER, payload=following, updated_at=STAMP),
        ])
        session.commit()
    ns.install_change_tracking(Session)
    changed = set()
    assert ns.repair_classifications(Session, AS_OF, changed=changed) > 0
    with Session() as session:
        repaired = session.query(ns.OwnershipReport).filter_by(code=CODE, report_date=SEPTEMBER).one()
        assert repaired.payload['social_entry']['status'] == 'comparable'
        assert repaired.payload['social_entry']['new_combinations'] == []
        assert repaired.payload['categories'] == following['categories']
        assert session.query(ns.Change).filter_by(code=CODE, record_date=SEPTEMBER).count() == 1
        assert ('持仓', CODE, SEPTEMBER) in changed


def test_repair_batch_failure_keeps_checkpoint_and_resumes(ns, database):
    engine, Session = database
    codes = ('002281.XSHE', '600001.XSHG')
    seed_reports(ns, Session, codes)
    ns.settings['stock_batch_size'] = 1
    ns.install_change_tracking(Session)
    fail = [True]
    def interrupt_second_batch(conn, cursor, statement, parameters, context, executemany):
        if fail[0] and statement.lstrip().upper().startswith('UPDATE OWNERSHIP_REPORT') and codes[1] in str(parameters):
            raise RuntimeError('synthetic second batch failure')
    event.listen(engine, 'before_cursor_execute', interrupt_second_batch)
    with pytest.raises(RuntimeError, match='synthetic second batch failure'):
        ns.repair_classifications(Session, AS_OF)
    with Session() as session:
        checkpoint = session.query(ns.DisclosureProbe).filter_by(key='classify-v1').one()
        assert checkpoint.payload == {'complete': False, 'last_code': codes[0]}
        first = session.query(ns.OwnershipReport).filter_by(code=codes[0], report_date=SEPTEMBER).one()
        second = session.query(ns.OwnershipReport).filter_by(code=codes[1], report_date=SEPTEMBER).one()
        assert first.payload['social_complete'] is True
        assert 'classification_version' not in second.payload
        assert session.query(ns.Change).filter_by(code=codes[1]).count() == 0
        revision = session.query(ns.SyncHead).one().revision
    fail[0] = False
    assert ns.repair_classifications(Session, AS_OF) > 0
    with Session() as session:
        assert session.query(ns.DisclosureProbe).filter_by(key='classify-v1').one().payload['complete'] is True
        assert session.query(ns.OwnershipReport).filter_by(code=codes[1], report_date=SEPTEMBER).one().payload['social_complete'] is True
        assert session.query(ns.SyncHead).one().revision > revision


@pytest.fixture
def gap_env(mod, tmp_path):
    base = declarative_base()
    class Floats(base):
        __tablename__ = 'floats'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        end_date = Column(Date)
        pub_date = Column(Date)
        change_reason_id = Column(Integer)
        sharesnature_id = Column(Integer)
        shareholder_rank = Column(Integer)
        shareholder_id = Column(String)
        shareholder_name = Column(String)
        shareholder_class_id = Column(Integer)
        share_number = Column(Float)
        share_ratio = Column(Float)
    source = create_engine('sqlite://')
    base.metadata.create_all(source)
    source_session = sessionmaker(bind=source)()
    calls, failures = [], []
    def run_query(q):
        calls.append(q.statement.compile().params)
        assert {t.name for t in q.statement.get_final_froms()} == {'floats'}
        if failures:
            raise RuntimeError('synthetic classification source failure')
        with source.connect() as conn:
            result = conn.execute(q.statement)
            return pd.DataFrame(result.fetchall(), columns=result.keys())
    finance = SimpleNamespace(STK_SHAREHOLDER_FLOATING_TOP10=Floats, run_query=run_query)
    n = mod._ownership_strategy({'finance': finance, 'query': source_session.query}, mod.DB_CONFIG)
    target = create_engine('sqlite:///' + str(tmp_path / 'gap-target.sqlite'))
    n.Base.metadata.create_all(target)
    Session = sessionmaker(bind=target)
    n.install_change_tracking(Session)
    try:
        yield SimpleNamespace(ns=n, Floats=Floats, source_session=source_session,
                              Session=Session, calls=calls, failures=failures)
    finally:
        source_session.close()
        target.dispose()
        source.dispose()


def filled_rows(period):
    rows = source_rows(period)
    for row in rows:
        name = row['shareholder_name']
        row['shareholder_class_id'] = (307004 if '社保' in name else 307003 if '证券投资基金' in name
                                       else 307001 if name == '卜浩文' else 307099)
    return rows


def rows_payload(ns, rows):
    members, complete, pub = ns.float_snapshot(rows, 2)
    payload = {'float_members': members, 'float_complete': complete, 'float_pub_date': pub,
               'categories': {'public_fund': {'shares': 100, 'market_value': 200,
                                            'holder_count': 1, 'scope': 'fund_disclosed', 'value_method': 'reported'}}}
    result = ns.classify_payload(payload)
    result['social_entry'] = ns.social_entry(result, None)
    return result


def add_gap_source(env, code, period, rows, offset=0):
    env.source_session.add_all([env.Floats(code=code, end_date=period, change_reason_id=306019,
                                         sharesnature_id=308007, **dict(row, id=row['id'] + offset)) for row in rows])
    env.source_session.commit()


def add_gap_report(env, code, period, rows):
    payload = rows_payload(env.ns, rows)
    with env.Session() as session:
        session.add(env.ns.OwnershipReport(code=code, report_date=period, payload=payload, updated_at=STAMP))
        session.commit()
    return payload


def gap_database_state(env):
    with env.Session() as session:
        reports = [(r.code, r.report_date, copy.deepcopy(r.payload), r.updated_at)
                   for r in session.query(env.ns.OwnershipReport).order_by(env.ns.OwnershipReport.code, env.ns.OwnershipReport.report_date)]
        revision = session.query(env.ns.SyncHead).one().revision
        journal = [(r.code, r.record_date, r.revision, r.deleted) for r in session.query(env.ns.Change).order_by(env.ns.Change.code, env.ns.Change.record_date)]
    return reports, revision, journal


def test_gap_refresh_repairs_source_class_only_and_recalculates_following_report(gap_env):
    env, code = gap_env, CODE
    prior_rows = filled_rows(JUNE)
    missing_rows = copy.deepcopy(prior_rows)
    for row in missing_rows:
        row['shareholder_class_id'] = None
    add_gap_report(env, code, dt.date(2026, 3, 31), prior_rows)
    old = add_gap_report(env, code, JUNE, missing_rows)
    following = add_gap_report(env, code, SEPTEMBER, filled_rows(SEPTEMBER))
    add_gap_source(env, code, JUNE, prior_rows)
    before_revision = gap_database_state(env)[1]
    changed = set()
    env.ns.refresh_classification_gaps(env.Session, AS_OF, changed)
    assert env.calls and all(call['end_date_1'] == JUNE and call['code_1'] == [code] for call in env.calls)
    with env.Session() as session:
        repaired = session.query(env.ns.OwnershipReport).filter_by(code=code, report_date=JUNE).one().payload
        next_report = session.query(env.ns.OwnershipReport).filter_by(code=code, report_date=SEPTEMBER).one().payload
        assert repaired['classification_pending'] is False and repaired['social_complete'] is True
        assert repaired['categories']['social']['shares'] == 1872908
        assert repaired['categories']['social']['complete'] is True
        assert repaired['categories']['public_fund'] == old['categories']['public_fund']
        assert repaired['float_pub_date'] == old['float_pub_date']
        assert [m['market_value'] for m in repaired['float_members']] == [m['market_value'] for m in old['float_members']]
        assert next_report['social_entry']['status'] == 'comparable'
        assert not next_report['social_entry']['new_combinations']
        assert next_report['categories'] == following['categories']
        assert social_increase(next_report, repaired)['change_shares'] == 1475929
        assert session.query(env.ns.SyncHead).one().revision > before_revision
        assert {JUNE, SEPTEMBER} <= {r.record_date for r in session.query(env.ns.Change).filter(env.ns.Change.revision > before_revision)}
        assert session.query(env.ns.DisclosureProbe).filter_by(key='classify-retry').one().as_of == AS_OF
    calls_before = len(env.calls)
    env.ns.refresh_classification_gaps(env.Session, AS_OF, set())
    assert len(env.calls) == calls_before


def test_gap_refresh_same_day_skips_and_next_day_missing_source_does_not_write_reports(gap_env):
    env = gap_env
    add_gap_report(env, CODE, SEPTEMBER, source_rows(SEPTEMBER))
    add_gap_source(env, CODE, SEPTEMBER, source_rows(SEPTEMBER))
    before = gap_database_state(env)
    changed = set()
    env.ns.refresh_classification_gaps(env.Session, AS_OF, changed)
    count = len(env.calls)
    assert count > 0 and not changed
    assert gap_database_state(env) == before
    env.ns.refresh_classification_gaps(env.Session, AS_OF, changed)
    assert len(env.calls) == count
    env.ns.refresh_classification_gaps(env.Session, AS_OF + dt.timedelta(days=1), changed)
    assert len(env.calls) > count
    assert gap_database_state(env) == before and not changed


@pytest.mark.parametrize('different', ['pub_date', 'share_number', 'share_ratio', 'shareholder_name', 'shareholder_rank'])
def test_gap_refresh_never_combines_metadata_from_different_snapshot(gap_env, different):
    env = gap_env
    add_gap_report(env, CODE, SEPTEMBER, source_rows(SEPTEMBER))
    fresh = filled_rows(SEPTEMBER)
    if different == 'pub_date':
        for row in fresh:
            row['pub_date'] = AS_OF - dt.timedelta(days=1)
    elif different in ('share_number', 'share_ratio'):
        fresh[0][different] += 1
    elif different == 'shareholder_name':
        fresh[0][different] = '另一个股东'
    else:
        fresh[0]['shareholder_rank'], fresh[1]['shareholder_rank'] = 2, 1
    add_gap_source(env, CODE, SEPTEMBER, fresh)
    before = gap_database_state(env)
    env.ns.refresh_classification_gaps(env.Session, AS_OF, set())
    assert env.calls and gap_database_state(env) == before


@pytest.mark.parametrize('batch_size,count,expected', [(1, 3, 1), (100, 51, 50)])
def test_gap_refresh_rotates_daily_and_caps_stock_scope(gap_env, batch_size, count, expected):
    env = gap_env
    env.ns.settings['stock_batch_size'] = batch_size
    codes = ['600{:03d}.XSHG'.format(i) for i in range(count)]
    for code in codes:
        add_gap_report(env, code, SEPTEMBER, source_rows(SEPTEMBER))
    # Records beyond the recent four quarters must not enter the retry queue.
    add_gap_report(env, '000001.XSHE', dt.date(2024, 9, 30), source_rows(SEPTEMBER))
    before = gap_database_state(env)
    env.ns.refresh_classification_gaps(env.Session, AS_OF, set())
    assert env.calls[0]['code_1'] == codes[:expected]
    env.calls.clear()
    env.ns.refresh_classification_gaps(env.Session, AS_OF + dt.timedelta(days=1), set())
    assert env.calls[0]['code_1'] == codes[expected:expected * 2]
    cycle_days = (count + expected - 1) // expected
    for day in range(2, cycle_days + 1):
        env.calls.clear()
        env.ns.refresh_classification_gaps(env.Session, AS_OF + dt.timedelta(days=day), set())
        start = (day % cycle_days) * expected
        assert env.calls[0]['code_1'] == codes[start:start + expected]
    assert gap_database_state(env) == before


def test_gap_source_failure_preserves_reports_and_retry_checkpoint(gap_env):
    env = gap_env
    add_gap_report(env, CODE, SEPTEMBER, source_rows(SEPTEMBER))
    with env.Session() as session:
        session.add(env.ns.DisclosureProbe(key='classify-retry', as_of=AS_OF - dt.timedelta(days=1),
                                          payload={'last_code': ''}))
        session.commit()
    before = gap_database_state(env)
    env.failures.append(True)
    with pytest.raises(RuntimeError, match='synthetic classification source failure'):
        env.ns.refresh_classification_gaps(env.Session, AS_OF, set())
    assert gap_database_state(env) == before
    with env.Session() as session:
        checkpoint = session.query(env.ns.DisclosureProbe).filter_by(key='classify-retry').one()
        assert checkpoint.as_of == AS_OF - dt.timedelta(days=1)
        assert checkpoint.payload == {'last_code': ''}


def test_gap_refresh_does_not_overwrite_concurrent_new_snapshot(gap_env):
    env = gap_env
    old = add_gap_report(env, CODE, SEPTEMBER, source_rows(SEPTEMBER))
    add_gap_source(env, CODE, SEPTEMBER, filled_rows(SEPTEMBER))
    newer = copy.deepcopy(old)
    newer['float_members'][0]['shares'] += 100
    newer['float_members'][0]['market_value'] += 200
    published = []
    original_query = env.ns.finance.run_query
    def query_and_publish(q):
        result = original_query(q)
        if not published:
            with env.Session() as session:
                row = session.query(env.ns.OwnershipReport).filter_by(code=CODE, report_date=SEPTEMBER).one()
                row.payload = newer
                row.updated_at = dt.datetime(2026, 10, 26, 12)
                session.commit()
            published.append(gap_database_state(env))
        return result
    env.ns.finance.run_query = query_and_publish
    changed = set()
    env.ns.refresh_classification_gaps(env.Session, AS_OF, changed)
    assert published and not changed
    assert gap_database_state(env) == published[0]


def test_repeated_raw_collection_does_not_invalidate_normalized_following_report(gap_env):
    env, n = gap_env, gap_env.ns
    source_base = declarative_base()
    class Fund(source_base):
        __tablename__ = 'fund'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        symbol = Column(String)
        period_end = Column(Date)
        pub_date = Column(Date)
        report_type_id = Column(Integer)
        report_type = Column(String)
        shares = Column(Float)
        market_cap = Column(Float)
    source_base.metadata.create_all(env.source_session.bind)
    n.finance.FUND_PORTFOLIO_STOCK = Fund
    original_query = n.finance.run_query
    def with_empty_fund_source(q):
        if {t.name for t in q.statement.get_final_froms()} == {'fund'}:
            with env.source_session.bind.connect() as conn:
                result = conn.execute(q.statement)
                return pd.DataFrame(result.fetchall(), columns=result.keys())
        return original_query(q)
    n.finance.run_query = with_empty_fund_source
    n._apis.update(get_all_securities=lambda **kwargs: pd.DataFrame(index=[CODE]),
                   get_trade_days=lambda **kwargs: [kwargs['end_date']],
                   get_price=lambda codes, **kwargs: pd.DataFrame({'code': codes, 'close': [2] * len(codes)}))
    add_gap_report(env, CODE, dt.date(2026, 3, 31), filled_rows(JUNE))
    raw = source_rows(JUNE)
    for row in raw:
        row['shareholder_class_id'] = None
    add_gap_source(env, CODE, JUNE, raw)
    with env.Session() as session:
        n.save_report_batch(session, [CODE], JUNE, AS_OF, {}, {}, set())
        session.commit()
        current = session.query(n.OwnershipReport).filter_by(code=CODE, report_date=JUNE).one().payload
        assert current['social_complete'] and current['classification_pending']
        assert all(m['class_id'] is not None and m['raw_class_id'] is None for m in current['float_members'])
        following = rows_payload(n, filled_rows(SEPTEMBER))
        following['social_entry'] = n.social_entry(following, current)
        assert following['social_entry']['status'] == 'comparable'
        session.add(n.OwnershipReport(code=CODE, report_date=SEPTEMBER, payload=following, updated_at=STAMP))
        session.commit()
    before = gap_database_state(env)
    changed = set()
    with env.Session() as session:
        n.save_report_batch(session, [CODE], JUNE, AS_OF, {}, {}, changed)
        session.commit()
        follower = session.query(n.OwnershipReport).filter_by(code=CODE, report_date=SEPTEMBER).one()
        assert follower.payload['social_entry'] == following['social_entry']
        assert follower.payload['social_entry']['status'] == 'comparable'
        assert follower.updated_at == STAMP
    assert not changed
    assert gap_database_state(env) == before  # No report writes, timestamp churn or journal revisions.


def test_json_boolean_mysql_compilation_avoids_new_sqlalchemy_json_accessors(ns):
    from sqlalchemy.dialects import mysql
    dialect = mysql.dialect()
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=dialect))
    for value, literal in ((True, 'true'), (False, 'false')):
        expression = ns.json_boolean(session, 'classification_pending', value)
        compiled = str(expression.compile(dialect=dialect, compile_kwargs={'literal_binds': True}))
        assert "json_unquote(json_extract(ownership_report.payload, '$.classification_pending'))" in compiled
        assert compiled.endswith(" = '{}'".format(literal))
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'), feature_version=(3, 6))
    assert not any(isinstance(node, ast.Attribute) and node.attr in ('as_boolean', 'as_string')
                   for node in ast.walk(tree))
