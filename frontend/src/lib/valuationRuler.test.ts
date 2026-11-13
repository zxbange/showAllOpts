import { describe, expect, it } from 'vitest'
import { valuationChartOption, valuationNumber, valuationSource } from './valuationRuler'
import type { ValuationHistory, ValuationPoint } from '../types/valuationRuler'
const point = (date: string, mean: number | null, source = 'joinquant'): ValuationPoint => ({
  date,
  index_close: mean == null ? null : 3500,
  mean_pe_ttm: mean,
  source,
  positive_count: 270,
  loss_count: 30,
  member_count: 300,
  gap: mean == null,
})
const data: ValuationHistory = {
  points: [
    point('2010-01-04', 10),
    point('2010-01-05', null),
    point('2026-11-09', 30),
    point('2026-11-10', 40, 'csi_em'),
  ],
  latest: null,
  method: 'csi300_equal_positive_pe_ttm_v1',
  benchmarks: {
    p20: 18,
    p80: 36,
    sample_days: 3,
    start_date: '2010-01-04',
    end_date: '2026-11-10',
  },
  coverage: {
    requested_start: '2010-01-01',
    first_date: '2010-01-04',
    last_date: '2026-11-10',
    missing_count: 1,
    known_missing_count: 1,
    calendar_complete: false,
  },
  gap_dates: ['2010-01-05'],
  sources: ['joinquant', 'csi_em'],
}
describe('valuation chart truthfulness', () => {
  it('shows missing PE separately from negative PE and retains the complete member count', () => {
    const option = valuationChartOption({ ...data, points: [{ ...data.points[0], loss_count: 29, missing_pe_count: 1 }] }, 0)
    const tooltip = option.tooltip.formatter([{ dataIndex: 0 }])
    expect(tooltip).toContain('有效正PE 270 / 负PE 29 / 缺失PE 1 / 成分 300')
  })
  it('uses server full-history quantiles on the right axis regardless of selected viewport', () => {
    for (const days of [0, 365, 1095]) {
      const option = valuationChartOption(data, days)
      expect(option.series.filter((s) => s.name === '历史P20')[0]).toMatchObject({
        yAxisIndex: 1,
        data: [18, 18, 18, 18],
      })
      expect(option.series.filter((s) => s.name === '历史P80')[0]).toMatchObject({
        yAxisIndex: 1,
        data: [36, 36, 36, 36],
      })
      expect(option.series.find((s) => s.name === '沪深300指数')?.yAxisIndex).toBe(0)
    }
  })
  it('never interpolates gaps or joins different sources', () => {
    const series = valuationChartOption(data, 0).series.filter((s) => s.name === '成分股平均PE-TTM')
    expect(series.map((s) => s.data)).toEqual([
      [10, null, null, null],
      [null, null, 30, null],
      [null, null, null, 40],
    ])
    for (const s of series) expect(s).toMatchObject({ smooth: false, connectNulls: false })
  })
  it('keeps unavailable benchmarks null and avoids fake zero displays', () => {
    const option = valuationChartOption(
      { ...data, benchmarks: { ...data.benchmarks, p20: null, p80: null } },
      0,
    )
    expect(option.series.at(-1)?.data).toEqual([null, null, null, null])
    expect(valuationNumber(null)).toBe('—')
    expect(valuationNumber(NaN)).toBe('—')
    expect(valuationNumber(0)).toBe('0.00')
    expect(valuationSource('unexpected_actual_source')).toBe('unexpected_actual_source')
  })
})
