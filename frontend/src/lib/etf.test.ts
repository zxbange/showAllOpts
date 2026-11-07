import { describe, expect, it } from 'vitest'
import {
  billionShares,
  chartSeries,
  marketValueSeries,
  showETFSymbols,
  collectionSummary,
  collectionErrorMessage,
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
  ).toContain('按批次补采')
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
