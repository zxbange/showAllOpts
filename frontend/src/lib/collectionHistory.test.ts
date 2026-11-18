import { expect, it } from 'vitest'
import { collectionHistoryModule, collectionHistoryText } from './collectionHistory'
it('selects the current source, not a neighboring tab or aggregated result', () => {
  expect(collectionHistoryModule('/commodities', '?instrument=cci')).toBe('commodities-cci')
  expect(collectionHistoryModule('/commodities', '')).toBe('commodities-a00')
  expect(collectionHistoryModule('/cash-cow', '?years=3')).toBe('cash-cow-3')
  expect(collectionHistoryModule('/cash-cow', '?years=bad')).toBe('cash-cow-10')
  expect(collectionHistoryModule('/dividend-selection', '?method=bank')).toBe('dividend-selection')
  expect(collectionHistoryModule('/industry-momentum/industries/1', '')).toBe('industry-momentum')
})
it('never queries history from overview, management pages, or duplicate ETF/social panels', () => {
  for (const path of ['/', '/system-configuration', '/account', '/social-monitor', '/etf-shares'])
    expect(collectionHistoryModule(path, '')).toBeNull()
  expect(collectionHistoryModule('/personal-holdings', '')).toBeNull()
  expect(collectionHistoryModule('/personal-holdings', '', true)).toBe('personal-holdings')
})
it('retains trigger, failure, cancellation, and database-sync semantics', () => {
  const record = {
    run_key: 'one',
    outcome: 'cancelled' as const,
    finished_at: '2026-11-18T10:00:00+08:00',
    trigger: 'manual' as const,
    kind: 'database_sync' as const,
  }
  expect(collectionHistoryText(record)).toContain('手动数据库同步 · 已取消')
  expect(
    collectionHistoryText({
      ...record,
      outcome: 'failed',
      trigger: 'scheduled',
      kind: 'collection',
    }),
  ).toContain('自动采集 · 失败')
  expect(collectionHistoryText({ ...record, finished_at: null })).toContain('完成时间未记录')
})
