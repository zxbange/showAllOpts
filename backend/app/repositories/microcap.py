from datetime import date
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.microcap import DEFAULT_SERIES, LEGACY_SERIES, MicrocapTemperature


class MicrocapRepository:
    def __init__(self, session: Session):
        self.session = session

    def available_series(self) -> list[str]:
        values = self.session.scalars(select(MicrocapTemperature.series_id).distinct())
        return sorted(value for value in values if self.supported_series(value))

    @staticmethod
    def supported_series(value: str) -> bool:
        if value in (DEFAULT_SERIES, LEGACY_SERIES):
            return True
        if not re.fullmatch(r'micro1000_1d_[0-9]{8}_v1_archive', value):
            return False
        try:
            date.fromisoformat(value.split('_')[2])
            return True
        except ValueError:
            return False

    def history(self, end: date, series_id: str | None = None) -> list[MicrocapTemperature]:
        if series_id is not None and not self.supported_series(series_id):
            raise ValueError('不支持的微盘序列。')
        selected = (series_id,) if series_id else (DEFAULT_SERIES, LEGACY_SERIES)
        # 两种序列都只有日级汇总；一条查询保留旧版回退，不读取成分股。
        rows = self.session.scalars(select(MicrocapTemperature).where(
            MicrocapTemperature.series_id.in_(selected),
            MicrocapTemperature.trade_date <= end,
        ).order_by(MicrocapTemperature.trade_date))
        series = {key: [] for key in selected}
        daily_pending = False
        for row in rows:
            if row.series_id != LEGACY_SERIES:
                # 0 是采集端原子保存的修复检查点；不展示尚未重新衔接的后续数据。
                daily_pending = daily_pending or row.sample_count == 0
                if daily_pending:
                    continue
            series[row.series_id].append(row)
        candidates = [items for items in series.values() if items]
        # 回填追上旧数据后优先日频；整条曲线不混用两种指数。
        return max(candidates, key=lambda items: (
            items[-1].trade_date, items[-1].series_id == DEFAULT_SERIES), default=[])
