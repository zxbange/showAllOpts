"""Validate a published ETF pool against the same connection's heat rows."""
from sqlalchemy import select
from app.models.etf_heat import Heat, Pool
from app.core.cache_errors import SnapshotDataError


class ETFMembersMissing(SnapshotDataError):
    def __init__(self, day, expected, missing):
        super().__init__('ETF/LOF 最新热度结果尚未完整入库，保留上一快照。')
        self.day, self.expected, self.missing = day, expected, missing


def validate_etf_snapshot(connection, require_pool=False):
    heads = connection.execute(select(Pool.__table__)).mappings().all()
    if not heads:
        if require_pool:
            raise SnapshotDataError('ETF/LOF 源候选池尚未发布，保留上一快照。')
        return None
    if len(heads) != 1 or heads[0]['id'] != 1:
        raise SnapshotDataError('ETF/LOF 候选池标识异常，保留上一快照。')
    head = heads[0]
    if head['rule_version'] not in ('etf_close_momentum_v1', 'etf_close_momentum_v2', 'etf_close_momentum_v3'):
        raise SnapshotDataError('请先在聚宽运行计算入库版 ETF/LOF 采集脚本，再同步热度结果。')
    members = head['members']
    if (not isinstance(members, list) or not members or
            any(not isinstance(m, dict) or not isinstance(m.get('code'), str) or
                not m['code'] or len(m['code']) > 16 for m in members)):
        raise SnapshotDataError('ETF/LOF 候选池成员为空或无效，保留上一快照。')
    codes = {m['code'] for m in members}
    if len(codes) != len(members):
        raise SnapshotDataError('ETF/LOF 候选池成员重复，保留上一快照。')
    present = set(connection.scalars(select(Heat.code).where(Heat.trade_date == head['as_of_date'])))
    missing = len(codes - present)
    if missing:
        raise ETFMembersMissing(head['as_of_date'], len(codes), missing)
    return head
