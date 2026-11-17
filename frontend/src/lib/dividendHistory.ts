import type { DividendOverview } from '../types/dividend'

export const DIVIDEND_HISTORY_RULE = 'dividend_dps_proposal_age3_annual_history_v10'

export function dividendHistoryIsLegacy(
  data: Pick<DividendOverview, 'snapshot' | 'requires_refresh'> | null,
) {
  return data?.requires_refresh === true || data?.snapshot?.rule_version !== DIVIDEND_HISTORY_RULE
}

export function dividendHistoryNote(legacy: boolean) {
  return legacy
    ? '旧版快照，沿用原股息率口径；新版采集成功后更新。'
    : '最近财年按最新方案分红和当日股价计算；此前两年按已实施每股分红和当年平均收盘价计算。'
}
