import { describe, expect, it } from 'vitest'
import {
  billionShares,
  chartSeries,
  marketValueSeries,
  showETFSymbols,
  collectionSummary,
  collectionErrorMessage,
  withManualRun,
} from './etf'
import type { ETFHistory } from '../types/etf'

describe('ETF chart dates and units', () => {
  it('aligns series by actual date, preserves zero and missing values', () => {
    const data = {
      funds: [
        {
          code: 'A',
          name: 'A',
          points: [
            { date: '2026-01-02', shares: '0', change_pct: null },
            { date: '2026-01-06', shares: '200000000', change_pct: 20 },
          ],
        },
        {
          code: 'B',
          name: 'B',
          points: [
            { date: '2026-01-05', shares: '100000000', change_pct: 0 },
            { date: '2026-01-06', shares: '150000000', change_pct: 50 },
          ],
        },
      ],
    } as ETFHistory
    const chart = chartSeries(data, false, null)
    expect(chart.dates).toEqual(['2026-01-02', '2026-01-05', '2026-01-06'])
    expect(chart.series.map((s) => s.values)).toEqual([
      [0, null, 2],
      [null, 1, 1.5],
    ])
    expect(chartSeries(data, true, 'A').series[0].values).toEqual([null, null, 20])
    expect(chartSeries(data, false, 'B').series[0].color).toBe(chart.series[1].color)
    expect(billionShares(null)).toBe('—')
    expect(billionShares('0')).toBe('0.00')
  })
})

describe('ETF aggregate market values', () => {
  it('shows one total in RMB hundred millions and preserves incomplete dates as gaps', () => {
    const data = {
      group_name: '沪深300 ETF',
      funds: [{ code: 'A', name: 'A', points: [{ date: '2026-01-04', shares: '200' }] }],
      market_values: [
        { date: '2026-01-05', market_value: '300000000', available_funds: 2, expected_funds: 2 },
        { date: '2026-01-02', market_value: '0', available_funds: 2, expected_funds: 2 },
        { date: '2026-01-03', market_value: null, available_funds: 1, expected_funds: 2 },
        { date: '2026-01-06', market_value: '120000000', available_funds: 1, expected_funds: 2 },
      ],
    } as ETFHistory
    const chart = marketValueSeries(data)
    expect(chart.dates).toEqual([
      '2026-01-02',
      '2026-01-03',
      '2026-01-04',
      '2026-01-05',
      '2026-01-06',
    ])
    expect(chart.series).toHaveLength(1)
    expect(chart.series[0].values).toEqual([0, null, null, 3, null])
  })

  it('supports legacy responses and rejects invalid values without fabricating zero', () => {
    expect(marketValueSeries({ funds: [] } as unknown as ETFHistory).series[0].values).toEqual([])
    const data = {
      funds: [],
      market_values: ['NaN', '', 'Infinity', '-1'].map((market_value, i) => ({
        date: `2026-01-0${i + 1}`,
        market_value,
        available_funds: 1,
        expected_funds: 1,
      })),
    } as unknown as ETFHistory
    expect(marketValueSeries(data).series[0].values).toEqual([null, null, null, null])
  })
})

it('keeps a sparse verified market-value point visible on a long history axis', () => {
  expect(showETFSymbols([...Array<null>(300).fill(null), 2])).toBe(true)
  expect(showETFSymbols(Array<number>(101).fill(2))).toBe(false)
})

it('does not render a flagged market value even if a stale response includes an amount', () => {
  const data = {
    funds: [],
    market_values: [
      {
        date: '2026-11-06',
        market_value: '100000000',
        expected_funds: 1,
        available_funds: 1,
        quality_flags: ['possible_share_unit_change'],
      },
    ],
  } as unknown as ETFHistory
  expect(marketValueSeries(data).series[0].values).toEqual([null])
})

it('never presents missing writer configuration or denied writes as running collection', () => {
  expect(collectionSummary({ status: 'missing_writer_configuration' })).toContain('未就绪')
  expect(
    collectionSummary({
      status: 'configured',
      worker_running: true,
      runs: [
        { slot: '2026-11-06T18:30:00+08:00', status: 'failed', error: 'writer_permission_denied' },
      ],
    }),
  ).toContain('权限不足')
  expect(
    collectionSummary({
      status: 'configured',
      worker_running: true,
      schedule: ['09:30', '11:30', '18:30'],
      target_sessions: 300,
    }),
  ).toContain('仅更新最新已收市交易日')
})

it('renders collection errors without raw internal codes', () => {
  expect(collectionErrorMessage('writer_permission_denied')).toContain('权限不足')
  expect(collectionErrorMessage('service_interrupted')).toContain('中断')
  expect(collectionErrorMessage('configuration_changed')).toBe(
    '数据库目标已变更，本次任务已中断；请重新配置后采集。',
  )
  expect(collectionErrorMessage('unknown_backend_code')).not.toContain('unknown_backend_code')
  expect(collectionErrorMessage('配置已被修改，请刷新')).toBe('配置已被修改，请刷新')
})

it('connection failures refer to database settings without a TLS option', () => {
  expect(collectionErrorMessage('writer_connection_failed')).toBe(
    '无法连接数据库，请检查网络、账号和数据库配置。',
  )
})

it('retains calendar dates with no shares instead of jumping across missing history', () => {
  const data = {
    funds: [
      {
        code: 'A',
        name: 'A',
        points: [
          { date: '2026-01-14', shares: '100000000', change_pct: 0 },
          { date: '2026-11-06', shares: '200000000', change_pct: 100 },
        ],
      },
    ],
    market_values: [
      { date: '2026-01-14', market_value: null },
      { date: '2026-01-15', market_value: null },
      { date: '2026-11-05', market_value: null },
      { date: '2026-11-06', market_value: null },
    ],
  } as ETFHistory
  const shares = chartSeries(data, false, null)
  expect(shares.dates).toEqual(['2026-01-14', '2026-01-15', '2026-11-05', '2026-11-06'])
  expect(shares.series[0].values).toEqual([1, null, null, 2])
  expect(chartSeries(data, true, 'A').series[0].values).toEqual([0, null, null, 100])
  expect(marketValueSeries(data).dates).toEqual(shares.dates)
})

it('distinguishes pending, restricted and exhausted history even when automatic collection is disabled', () => {
  const summary = collectionSummary({
    status: 'disabled',
    history_complete: false,
    pending_count: 12,
    blocked_count: 3,
    retry_exhausted_count: 2,
  })
  expect(summary).toContain('自动采集已关闭')
  expect(summary).not.toContain('历史数据未采齐')
  expect(summary).toContain('本轮目标日仍有 12 项待采集')
  expect(summary).toContain('3 项受数据源可用性或核验限制')
  expect(summary).toContain('2 项已达到自动重试上限')
  expect(
    collectionSummary({
      status: 'disabled',
      manual: { id: 'job', status: 'partial', history_complete: false, pending_count: 4 },
    }),
  ).toContain('本轮目标日仍有 4 项待采集')
})

it('clears stale progress after terminal manual responses but retains active progress', () => {
  const status = {
    status: 'running',
    configured: true,
    enabled: false,
    progress: { phase: 'fetching', completed: 1, total: 10 },
  }
  for (const terminal of ['cancelled', 'complete', 'partial', 'failed', 'interrupted']) {
    const next = withManualRun(status, { id: 'job', status: terminal })
    expect(next.status).toBe('disabled')
    expect(next.progress).toBeNull()
    expect(next.manual?.status).toBe(terminal)
  }
  for (const active of ['queued', 'running', 'cancelling']) {
    expect(withManualRun(status, { id: 'job', status: active }).progress).toEqual(status.progress)
  }
})

it('separates daily interface collection from JoinQuant history starting in 2024', () => {
  const summary = collectionSummary({
    status: 'configured',
    worker_running: true,
    collection_scope: 'latest_completed_session',
    history_complete: false,
    target_sessions: 300,
    schedule: ['09:00'],
  })
  expect(summary).toContain('仅更新最新已收市交易日')
  expect(summary).toContain('2024-01-01')
  expect(summary).toContain('日常采集完成不代表历史齐全')
  expect(summary).not.toContain('历史目标 300')
  expect(collectionSummary({ status: 'disabled', retry_exhausted_count: 2 })).toContain(
    '勾选重试仅重置当前日常目标，不回补历史',
  )
})
