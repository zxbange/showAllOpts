import { describe, expect, it } from 'vitest'
import {
  calendarYearsAgo,
  growthDividendSeries,
  growthDividendSpan,
  growthDividendValue,
} from './growthDividend'
import type { GrowthDividendPoint } from '../types/growthDividend'

const points: GrowthDividendPoint[] = [
  '2018-01-02',
  '2020-01-02',
  '2021-11-09',
  '2021-11-10',
  '2026-11-10',
].map((date, index) => ({
  date,
  chinext: 2000,
  dividend: 4000,
  star50: index ? 1000 : null,
  chinext_dividend_ratio: 0.5,
  star50_dividend_ratio: index ? 0.25 : null,
}))

describe('growth / dividend date windows', () => {
  it('uses five calendar years ending on the latest observation and keeps all history', () => {
    expect(growthDividendSpan(points, 5)).toEqual({ start: '2021-11-10', end: '2026-11-10' })
    expect(growthDividendSpan(points, 0)).toEqual({ start: '2018-01-02', end: '2026-11-10' })
    expect(points).toHaveLength(5)
    expect(calendarYearsAgo('2024-02-29', 5)).toBe('2019-02-28')
  })

  it('selects actual dates without inventing observations or accepting empty windows', () => {
    expect(growthDividendSpan(points, 0, { start: '2019-12-31', end: '2021-11-09' })).toEqual({
      start: '2020-01-02',
      end: '2021-11-09',
    })
    expect(growthDividendSpan(points, 0, { start: '2021-11-10', end: '2021-11-09' })).toBeNull()
    expect(growthDividendSpan(points, 0, { start: '2020-01-04', end: '2020-01-05' })).toBeNull()
    expect(growthDividendSpan(points, 0, { start: '2027-01-01', end: '2027-01-02' })).toBeNull()
    expect(growthDividendSpan([], 5)).toBeNull()
  })

  it('anchors year presets to actual data while all/custom windows retain trailing missing days', () => {
    const missing: GrowthDividendPoint = {
      date: '2026-11-11',
      chinext: null,
      dividend: null,
      star50: null,
      chinext_dividend_ratio: null,
      star50_dividend_ratio: null,
    }
    const withGap = [...points, missing]
    expect(growthDividendSpan(withGap, 5)).toEqual({ start: '2021-11-10', end: '2026-11-10' })
    expect(growthDividendSpan(withGap, 0)).toEqual({ start: '2018-01-02', end: '2026-11-11' })
    expect(growthDividendSpan(withGap, 5, { start: '2026-11-10', end: '2026-11-11' })).toEqual({
      start: '2026-11-10',
      end: '2026-11-11',
    })
    expect(growthDividendSpan([...points, { ...missing, chinext: 3000 }], 5)?.end).toBe(
      '2026-11-11',
    )
    expect(growthDividendSpan([missing], 5)).toBeNull()
    expect(growthDividendSpan([missing], 0)).toEqual({ start: missing.date, end: missing.date })
  })
})

it('renders raw index levels on the left and unscaled ratios as dashed lines on the right', () => {
  const series = growthDividendSeries(points)
  expect(series.map((line) => [line.name, line.yAxisIndex, line.lineStyle.type])).toEqual([
    ['创业板指', 0, 'solid'],
    ['中证红利', 0, 'solid'],
    ['科创50', 0, 'solid'],
    ['创红比', 1, 'dashed'],
    ['科创红利比', 1, 'dashed'],
  ])
  expect(new Set(series.map((line) => line.lineStyle.color)).size).toBe(5)
  expect(series[0].data[0]).toBe(2000)
  expect(series[3].data[0]).toBe(0.5)
  expect(series[2].data[0]).toBeNull()
  expect(series[4].data[0]).toBeNull()
  expect(series.every((line) => !line.connectNulls)).toBe(true)
  expect(growthDividendValue(null, true)).toBe('—')
  expect(growthDividendValue(0.1234567, true)).toBe('0.1235')
})
