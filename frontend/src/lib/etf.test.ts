import { describe, expect, it } from 'vitest'
import { billionShares, chartSeries } from './etf'
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
