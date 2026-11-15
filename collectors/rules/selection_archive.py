from sqlalchemy import Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy import inspect

# BEGIN SHARED SELECTION ARCHIVE
def _selection_archive(_apis, _config, Base, Snapshot, Stock, rule_version, table_name,
                       date_field, calculate, validate_records, save_latest):
    """共享选股归档；计算与校验由策略提供，最新表和档案表分别提交。"""
    get_trade_days = _apis.get('get_trade_days')
    ArchiveBase = declarative_base()

    class SelectionArchive(ArchiveBase):
        __tablename__ = table_name
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        as_of_date = Column(Date, primary_key=True)
        source_kind = Column(String(32), primary_key=True)
        rule_version = Column(String(48), primary_key=True)
        status = Column(String(16), nullable=False)
        captured_at = Column(DateTime, nullable=False)
        snapshot_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
        stocks_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
        provenance_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
        error_code = Column(String(80), nullable=True)

    REVISION_LIMIT = ('聚宽披露差错修正只保留最新版本，pub_date 过滤不能恢复被覆盖版本；'
                      '不能可靠还原原始时点选股。仅可显式选择 revised_asof 保存修订数据重算档案。')
    SOURCE_URL = 'https://www.joinquant.com/community/post/detailMobile?postId=21247'

    def archive_json(value):
        def encode(item):
            if isinstance(item, (datetime.date, datetime.datetime, Decimal)):
                return str(item)
            raise TypeError('不支持的档案数据类型。')
        return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True, allow_nan=False)

    def validate_archive_schema(Session):
        with session_scope(Session) as session:
            engine = session.get_bind()
            schema = inspect(engine)
            columns = {col['name']: col for col in schema.get_columns(SelectionArchive.__tablename__)}
            expected = SelectionArchive.__table__.columns
            primary = schema.get_pk_constraint(SelectionArchive.__tablename__).get('constrained_columns', [])
            if set(columns) != set(expected.keys()) or set(primary) != {c.name for c in expected if c.primary_key}:
                raise ValueError('档案表字段或主键不兼容，未修改现有表。')
            for col in expected:
                actual = columns[col.name]
                if (actual['type']._type_affinity != col.type._type_affinity
                        or actual['nullable'] != col.nullable
                        or (isinstance(col.type, String) and col.type.length is not None
                            and (actual['type'].length is None or actual['type'].length < col.type.length))
                        or (engine.dialect.name == 'mysql' and isinstance(col.type, Text)
                            and not isinstance(actual['type'], LONGTEXT))):
                    raise ValueError('档案表类型、长度或空值约束不兼容，未修改现有表。')

    def save_archive(Session, day, source_kind, meta=None, records=None, error=None):
        validate_archive_schema(Session)
        if source_kind not in ('observed', 'revised_asof'):
            raise ValueError(REVISION_LIMIT)
        if error is None:
            if meta[date_field] != day or meta['rule_version'] != rule_version:
                raise ValueError('档案日期或规则不一致。')
            validate_records(meta, records)
        with session_scope(Session) as session:
            saved = session.query(SelectionArchive).filter_by(
                as_of_date=day, source_kind=source_kind, rule_version=rule_version).with_for_update().first()
            # Successful archives are immutable, including empty valid selections.
            if saved is not None and saved.status == 'complete':
                return False
            if saved is None:
                saved = SelectionArchive(as_of_date=day, source_kind=source_kind, rule_version=rule_version)
                session.add(saved)
            saved.status = 'gap' if error is not None else 'complete'
            saved.captured_at = china_now()
            saved.snapshot_json = archive_json(meta) if error is None else None
            saved.stocks_json = archive_json(records) if error is None else None
            saved.provenance_json = archive_json(dict(provider='JoinQuant', schema_version=1,
                rule_version=rule_version, source_kind=source_kind, original_point_in_time=False,
                limitation=REVISION_LIMIT, documentation=SOURCE_URL))
            # Never persist raw exceptions, which can contain credentials or SQL parameters.
            saved.error_code = type(error).__name__[:80] if error is not None else None
            session.commit()
            return True

    def archive_history(start_date=None, end_date=None, max_days=20, source_kind='revised_asof',
                        dry_run=False, allow_partial=False, retry_gaps=False):
        if source_kind != 'revised_asof':
            raise ValueError(REVISION_LIMIT)
        if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= 250):
            raise ValueError('max_days 必须为 None 或 1 到 250 的整数。')
        if not builtins.all(isinstance(v, bool) for v in (dry_run, allow_partial, retry_gaps)):
            raise ValueError('dry_run、allow_partial、retry_gaps 必须为布尔值。')
        now = china_now()
        calendar_api = _apis.get('get_all_trade_days')
        if not callable(calendar_api):
            raise ValueError('历史恢复需要 get_all_trade_days 核验完整交易日历。')
        full_calendar = calendar_api()
        if full_calendar is None:
            raise ValueError('完整交易日历缺失。')
        calendar = [as_date(value) for value in full_calendar]
        if not calendar or calendar != sorted(set(calendar)):
            raise ValueError('完整交易日历为空、重复或乱序。')
        start = as_date(start_date) if start_date is not None else calendar[0]
        end = as_date(end_date) if end_date is not None else now.date()
        if start > end or end > now.date():
            raise ValueError('日期范围无效或晚于今天。')
        end = _closed_day(end, now)
        raw_days = get_trade_days(start_date=start, end_date=end)
        if raw_days is None:
            raise ValueError('交易日历缺失。')
        days = [as_date(value) for value in raw_days]
        if days != sorted(set(days)) or builtins.any(not start <= day <= end for day in days):
            raise ValueError('交易日历重复、乱序或越界。')
        if days != [day for day in calendar if start <= day <= end]:
            raise ValueError('区间交易日历与完整交易日历不一致，拒绝漏日完成。')
        if not days:
            raise ValueError('区间内无交易日，未建立完成标记。')
        summary = dict(source_kind=source_kind, original_point_in_time=False, limitation=REVISION_LIMIT,
                       dates=[str(day) for day in days], added=0, skipped=0, gaps=[], remaining=0, unresolved_gaps=None)
        if dry_run:
            summary['remaining'] = len(days)
            return summary
        with database_scope(ArchiveBase, _config) as Session:
            validate_archive_schema(Session)
            with session_scope(Session) as session:
                previous = session.query(SelectionArchive).filter(
                    SelectionArchive.as_of_date >= start, SelectionArchive.as_of_date <= end,
                    SelectionArchive.source_kind == source_kind, SelectionArchive.rule_version == rule_version).all()
                done = {row.as_of_date for row in previous if row.status == 'complete'}
                gaps = {row.as_of_date: row.error_code for row in previous if row.status == 'gap'}
                if builtins.any(row.status not in ('complete', 'gap') for row in previous):
                    raise ValueError('档案存在未知状态，拒绝跳过。')
            summary['previous_gaps'] = [dict(date=str(day), error_code=gaps[day]) for day in sorted(gaps)]
            pending = [day for day in days if day not in done and (retry_gaps or day not in gaps)]
            limit = len(pending) if max_days is None else max_days
            summary['skipped'], summary['remaining'] = len(done), builtins.max(0, len(pending)-limit)
            for day in _progress_items(pending[:limit], '修订数据历史档案'):
                try:
                    meta, records = calculate(day)
                    if save_archive(Session, day, source_kind, meta, records):
                        summary['added'] += 1
                except Exception as exc:
                    save_archive(Session, day, source_kind, error=exc)
                    summary['gaps'].append(dict(date=str(day), error_code=type(exc).__name__))
                    if not allow_partial:
                        raise RuntimeError('{} 档案采集失败；该日保留缺口，已完成日期可续跑。'.format(day)) from None
            with session_scope(Session) as session:
                summary['unresolved_gaps'] = session.query(SelectionArchive).filter(
                    SelectionArchive.as_of_date >= start, SelectionArchive.as_of_date <= end,
                    SelectionArchive.source_kind == source_kind, SelectionArchive.rule_version == rule_version,
                    SelectionArchive.status == 'gap').count()
        return summary

    def restore_observed(day):
        """Restore an actual captured run, never silently promote a historical reconstruction."""
        day = as_date(day)
        with database_scope(ArchiveBase, _config) as Session:
            validate_archive_schema(Session)
            with session_scope(Session) as session:
                saved = session.query(SelectionArchive).filter_by(as_of_date=day,
                    source_kind='observed', rule_version=rule_version, status='complete').first()
                if saved is None:
                    raise ValueError('没有该日实际采集档案；修订重算不能冒充原始最新名单。')
                meta, records = json.loads(saved.snapshot_json), json.loads(saved.stocks_json)
        def decode(values, table):
            result = dict(values)
            if set(result) != {c.name for c in table.columns if c.name not in ('id', 'updated_at')}:
                raise ValueError('档案字段与当前表结构不一致。')
            for col in table.columns:
                value = result.get(col.name)
                if value is not None and isinstance(col.type, Date):
                    result[col.name] = as_date(value)
                elif value is not None and isinstance(col.type, Numeric):
                    result[col.name] = Decimal(value)
            return result
        meta = decode(meta, Snapshot.__table__)
        records = [decode(record, Stock.__table__) for record in records]
        with database_scope(Base, _config) as Session:
            return save_latest(Session, meta, records)
    return SimpleNamespace(ArchiveBase=ArchiveBase, SelectionArchive=SelectionArchive,
        archive_json=archive_json, validate_archive_schema=validate_archive_schema,
        save_archive=save_archive, archive_history=archive_history, restore_observed=restore_observed,
        REVISION_LIMIT=REVISION_LIMIT, SOURCE_URL=SOURCE_URL)
# END SHARED SELECTION ARCHIVE
