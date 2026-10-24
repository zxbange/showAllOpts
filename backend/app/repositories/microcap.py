from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.microcap import DEFAULT_SERIES, LEGACY_SERIES, MicrocapTemperature


class MicrocapRepository:
    def __init__(self, session: Session):
        self.session = session

    def history(self, end: date) -> list[MicrocapTemperature]:
        # 两种序列都只有日级汇总；一条查询保留旧版回退，不读取成分股。
        rows = self.session.scalars(select(MicrocapTemperature).where(
            MicrocapTemperature.series_id.in_((DEFAULT_SERIES, LEGACY_SERIES)),
            MicrocapTemperature.trade_date <= end,
        ).order_by(MicrocapTemperature.trade_date))
        series = {DEFAULT_SERIES: [], LEGACY_SERIES: []}
        daily_pending = False
        for row in rows:
            if row.series_id == DEFAULT_SERIES:
                # 0 是采集端原子保存的修复检查点；不展示尚未重新衔接的后续数据。
                daily_pending = daily_pending or row.sample_count == 0
                if daily_pending:
                    continue
            series[row.series_id].append(row)
        candidates = [items for items in series.values() if items]
        # 回填追上旧数据后优先日频；整条曲线不混用两种指数。
        return max(candidates, key=lambda items: (
            items[-1].trade_date, items[-1].series_id == DEFAULT_SERIES), default=[])
