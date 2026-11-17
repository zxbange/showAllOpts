import { expect, it } from 'vitest'
import {
  DIVIDEND_HISTORY_RULE,
  dividendHistoryIsLegacy,
  dividendHistoryNote,
} from './dividendHistory'

it('requires the precise annual-history version, including retained v9 snapshots', () => {
  for (const rule_version of ['dividend_dps_proposal_age3_v9', 'older', '', undefined]) {
    const data = { snapshot: { rule_version }, requires_refresh: false } as Parameters<
      typeof dividendHistoryIsLegacy
    >[0]
    expect(dividendHistoryIsLegacy(data)).toBe(true)
  }
  const data = { snapshot: { rule_version: DIVIDEND_HISTORY_RULE } } as Parameters<
    typeof dividendHistoryIsLegacy
  >[0]
  expect(dividendHistoryIsLegacy(data)).toBe(false)
  expect(dividendHistoryIsLegacy({ ...data!, requires_refresh: true })).toBe(true)
  expect(dividendHistoryIsLegacy(null)).toBe(true)
})

it('does not describe legacy values using the new annual-average denominator', () => {
  expect(dividendHistoryNote(true)).toContain('旧版快照')
  expect(dividendHistoryNote(true)).not.toContain('当年平均收盘价')
  expect(dividendHistoryNote(false)).toContain('当年平均收盘价')
  expect(dividendHistoryNote(false)).toContain('最新方案分红')
})
