import { describe, expect, it } from 'vitest'
import {
  crowdingSourceLabel,
  heatDenominator,
  heatSourceLabel,
  marketCollectionActive,
  marketCollectionErrorMessage,
  marketCollectionNeedsReload,
  marketSourceSegments,
  marketResultLabel,
} from './marketCollection'
import type { MarketCollectionStatus, MarketPointSource } from '../types/marketCollection'

const complete: MarketCollectionStatus = {
  enabled: true,
  configured: true,
  status: 'complete',
  timezone: 'Asia/Shanghai',
  schedule: ['18:30'],
  trade_date: '2026-11-10',
  run_id: 'one',
  total: 5000,
  completed: 5000,
  pending: 0,
  above_count: 1000,
  heat_status: 'written',
  crowding_status: 'written',
  cache_pending: false,
  updated_at: '2026-11-10T19:30:00+08:00',
  error: null,
}

describe('market publication state', () => {
  it('waits for a terminal published result and does not reload for every progress or idle poll', () => {
    const running = {
      ...complete,
      status: 'running',
      completed: 100,
      pending: 4900,
      heat_status: 'pending',
    }
    const progressed = { ...running, completed: 200, updated_at: '2026-11-10T18:05:00+08:00' }
    const caching = { ...complete, cache_pending: true }
    expect(marketCollectionNeedsReload(null, complete)).toBe(true)
    expect(marketCollectionNeedsReload(null, running)).toBe(false)
    expect(marketCollectionNeedsReload(null, caching)).toBe(false)
    expect(marketCollectionNeedsReload(running, progressed)).toBe(false)
    expect(marketCollectionNeedsReload(progressed, caching)).toBe(false)
    expect(marketCollectionNeedsReload(caching, complete)).toBe(true)
    expect(marketCollectionNeedsReload(complete, { ...complete })).toBe(false)
    expect(marketCollectionNeedsReload(complete, { ...complete, status: 'idle' })).toBe(false)
  })

  it('recognizes a new same-day result, including independent crowding publication', () => {
    expect(marketCollectionNeedsReload(complete, { ...complete, run_id: 'two' })).toBe(true)
    expect(
      marketCollectionNeedsReload(complete, {
        ...complete,
        updated_at: '2026-11-10T20:00:00+08:00',
      }),
    ).toBe(true)
    const running = {
      ...complete,
      status: 'running',
      heat_status: 'pending',
      crowding_status: 'pending',
    }
    expect(
      marketCollectionNeedsReload(running, {
        ...complete,
        status: 'partial',
        heat_status: 'blocked',
        crowding_status: 'written',
        pending: 2,
      }),
    ).toBe(true)
    expect(
      marketCollectionNeedsReload(running, {
        ...complete,
        status: 'failed',
        heat_status: 'blocked',
        crowding_status: 'pending',
      }),
    ).toBe(false)
  })
})

describe('recorded market methodology', () => {
  it('defaults to the original eligible denominator and preserves explicit historical methodology', () => {
    expect(heatDenominator({})).toBe('eligible')
    expect(heatDenominator({ source: 'sina' })).toBe('eligible')
    expect(heatSourceLabel({})).toBe('聚宽历史 · 前复权MA20')
    const canonical: MarketPointSource = {
      source: 'sina',
      price_basis: 'qfq',
      denominator: 'eligible',
    }
    expect(heatDenominator(canonical)).toBe('eligible')
    expect(heatSourceLabel(canonical)).toBe('新浪接口 · 前复权MA20')
    const sina: MarketPointSource = { source: 'sina', price_basis: 'bfq', denominator: 'universe' }
    expect(heatDenominator(sina)).toBe('universe')
    expect(heatSourceLabel(sina)).toBe('新浪接口 · 不复权MA20')
    expect(heatSourceLabel({ source: 'sina' })).toBe('新浪接口 · MA20')
    expect(crowdingSourceLabel(sina)).toBe('新浪接口 · 全A成交额')
    expect(crowdingSourceLabel({})).toBe('聚宽历史 · 全A成交额')
  })

  it('splits consecutive source/method segments without joining later legacy points across Sina data', () => {
    const points: MarketPointSource[] = [
      {},
      { source: 'joinquant', price_basis: 'qfq', denominator: 'eligible' },
      { source: 'sina', price_basis: 'bfq', denominator: 'universe' },
      { source: 'sina', price_basis: 'bfq', denominator: 'universe' },
      {},
    ]
    expect(marketSourceSegments(points, 'heat')).toEqual([
      { start: 0, end: 1 },
      { start: 2, end: 3 },
      { start: 4, end: 4 },
    ])
    expect(marketSourceSegments([], 'heat')).toEqual([])
    expect(
      marketSourceSegments(
        [
          { source: 'sina', denominator: 'universe' },
          { source: 'sina', denominator: 'eligible' },
        ],
        'heat',
      ),
    ).toHaveLength(2)
    expect(marketSourceSegments(points, 'crowding')).toHaveLength(3)
  })
})

it('explains missing MA20 and throttling without exposing unknown technical errors', () => {
  expect(marketCollectionErrorMessage('unknown_missing_ma')).toContain('暂不发布当日比例')
  expect(marketCollectionErrorMessage('source_rate_limited')).toContain('已保留进度')
  expect(marketCollectionErrorMessage('password=private-value')).not.toContain('private-value')
  expect(marketCollectionErrorMessage('配置已变更，请重新读取')).toBe('配置已变更，请重新读取')
  expect(marketCollectionActive({ ...complete, status: 'running', worker_disabled: true })).toBe(
    false,
  )
  expect(marketCollectionErrorMessage('worker_disabled')).toContain('服务端采集任务未启用')
  expect(marketCollectionErrorMessage('snapshot_quote_mismatch')).toContain(
    '当天股票列表与行情价量不一致',
  )
  expect(marketCollectionErrorMessage('historical_snapshot_unverified')).toContain(
    '不能用今天行情补历史',
  )
  expect(marketCollectionErrorMessage('heat_methodology_mismatch')).toContain('不作为新热度使用')
  expect(marketCollectionErrorMessage('heat_methodology_unverified')).toContain(
    '尚未通过聚宽原脚本口径核验',
  )
  expect(marketCollectionErrorMessage('writer_methodology_mismatch')).toBe(
    '本次结果的统计口径未通过校验，未写入；已有数据保留。',
  )
  expect(marketCollectionErrorMessage('writer_value_mismatch')).toBe(
    '本次结果的计数、比例或金额不一致，未写入；已有数据保留。',
  )
})

it('keeps public market sources distinct from legacy Sina and JoinQuant', () => {
  const point: MarketPointSource = { source: 'public_market', price_basis: 'qfq', denominator: 'eligible' }
  expect(heatSourceLabel(point)).toBe('公开行情 · 前复权MA20')
  expect(crowdingSourceLabel(point)).toBe('通达信日包 · 沪深股票及CDR成交额')
  expect(marketSourceSegments([{ source: 'sina' }, point], 'crowding')).toHaveLength(2)
  expect(marketCollectionErrorMessage('adjustment_unavailable')).toContain('复权因子')
})

it('labels out-of-window metrics without claiming publication', () => {
  expect(marketResultLabel('outside_window')).toBe('不在本次补缺范围')
})
