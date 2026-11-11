import { describe, expect, it } from 'vitest'
import { graphic, init, use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, MarkLineComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import {
  calendarYearsAgo,
  growthDividendBenchmarks,
  growthDividendSeries,
  growthDividendRatioExtent,
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

describe('full-history percentile benchmarks', () => {
  const sample = (
    date: string,
    chinext: number | null,
    star50: number | null = null,
  ): GrowthDividendPoint => ({
    ...points[0],
    date,
    chinext_dividend_ratio: chinext,
    star50_dividend_ratio: star50,
  })

  it('interpolates independent historical samples and excludes pre-2018 ChiNext values', () => {
    const history = [
      sample('2017-12-29', 9999),
      sample('2018-01-02', 0.5),
      sample('2019-12-31', 0.1),
      sample('2020-01-02', 0.4, 1),
      sample('2022-01-04', 0.2, 0.2),
      sample('2026-11-10', 0.3, 0.6),
    ]
    const original = structuredClone(history)
    const [chinext, star50] = growthDividendBenchmarks(history)
    expect(chinext.sampleCount).toBe(5)
    expect(chinext.levels.map((level) => level.percentile)).toEqual(['P20', 'P80'])
    expect(chinext.levels[0].value).toBeCloseTo(0.18, 12)
    expect(chinext.levels[1].value).toBeCloseTo(0.42, 12)
    expect(star50.sampleCount).toBe(3)
    expect(star50.levels[0].value).toBeCloseTo(0.36, 12)
    expect(star50.levels[1].value).toBeCloseTo(0.84, 12)
    expect(history).toEqual(original)
  })

  it('ignores missing, non-finite and non-positive ratios without creating zero samples', () => {
    const invalid = [null, NaN, Infinity, -Infinity, 0, -0.2].map((value) =>
      sample('2020-01-02', value, value),
    )
    expect(growthDividendBenchmarks(invalid)).toEqual([])
    expect(growthDividendBenchmarks([])).toEqual([])
    const [single] = growthDividendBenchmarks([...invalid, sample('2026-11-10', 0.3)])
    expect(single).toMatchObject({
      key: 'chinext_dividend_ratio',
      sampleCount: 1,
      levels: [{ percentile: 'P20/P80', value: 0.3 }],
    })
    expect(growthDividendSeries(invalid).filter((line) => line.markLine?.data.length)).toEqual([])
    const constant = growthDividendSeries([sample('2020-01-02', 0.3), sample('2026-11-10', 0.3)])
    expect(constant[3].markLine?.data).toMatchObject([
      {
        name: '创红比 P20/P80 0.3000',
        yAxis: 0.3,
        label: { formatter: '创红比\nP20/P80\n0.3000' },
      },
    ])
    expect(constant[4].markLine?.data).toEqual([])
  })

  it('updates when observations are corrected or added, but ignores newly missing dates', () => {
    const history = [sample('2018-01-02', 0.2), sample('2026-11-10', 0.8)]
    const before = growthDividendBenchmarks(history)
    expect(before[0].levels[0].value).toBeCloseTo(0.32, 12)
    expect(before[0].levels[1].value).toBeCloseTo(0.68, 12)
    expect(growthDividendBenchmarks([...history, sample('2026-11-11', null)])).toEqual(before)
    const after = growthDividendBenchmarks([
      history[0],
      sample('2026-11-10', 0.4),
      sample('2026-11-11', 0.6),
    ])
    expect(after[0].sampleCount).toBe(3)
    expect(after[0].levels[0].value).toBeCloseTo(0.28, 12)
    expect(after[0].levels[1].value).toBeCloseTo(0.52, 12)
    const series = growthDividendSeries(history, before)
    expect(series.slice(0, 3).every((line) => line.markLine === undefined)).toBe(true)
    expect(series[3].markLine?.lineStyle).toMatchObject({ type: 'solid', width: 2.4, opacity: 0.9 })
    expect(series[3].markLine?.label).toMatchObject({ show: true, opacity: 1 })
    expect(series[3].markLine?.data.map((line) => line.label)).toEqual([
      { position: 'insideStartBottom', formatter: '创红比\nP20 0.3200' },
      { position: 'insideStartTop', formatter: '创红比\nP80 0.6800' },
    ])
    expect(series[3].markLine?.data.map((line) => line.yAxis)).toEqual(
      before[0].levels.map((level) => level.value),
    )
  })
})

describe('on-chart percentile labels', () => {
  use([LineChart, GridComponent, MarkLineComponent, SVGRenderer])

  const scenarios = [
    { name: 'distinct ratios', ratios: [0.5, 0.55, 0.6], starRatios: [0.2, 0.23, 0.25] },
    {
      name: 'nearly coincident references',
      ratios: [0.1, 0.3, 0.3, 0.3, 0.30000001, 0.30000002, 0.9],
      starRatios: [0.1, 0.3, 0.3, 0.3, 0.30000001, 0.30000002, 0.9],
    },
    { name: 'merged equal references', ratios: [0.3, 0.3], starRatios: [0.3, 0.3] },
  ]

  for (const width of [280, 320, 960]) {
    for (const scenario of scenarios) {
      it(`renders complete, separated labels at ${width}px for ${scenario.name}`, () => {
        const history = scenario.ratios.map((ratio, index) => ({
          ...points[0],
          date: `2026-11-${String(index + 1).padStart(2, '0')}`,
          chinext_dividend_ratio: ratio,
          star50_dividend_ratio: scenario.starRatios[index],
        }))
        const benchmarks = growthDividendBenchmarks(history)
        const referenceValues = benchmarks.flatMap((benchmark) =>
          benchmark.levels.map((level) => level.value),
        )
        const ratioExtent = (range: { min: number; max: number }) =>
          growthDividendRatioExtent(range, referenceValues)
        const chart = init(null, undefined, { renderer: 'svg', ssr: true, width, height: 390 })
        try {
          chart.setOption({
            animation: false,
            grid: { left: 10, right: 10, top: 40, bottom: 78, containLabel: true },
            xAxis: { type: 'category', data: history.map((point) => point.date) },
            yAxis: [
              { type: 'value', scale: true, axisLabel: { fontSize: 11 } },
              {
                type: 'value',
                scale: true,
                min: (range: { min: number; max: number }) => ratioExtent(range)?.min,
                max: (range: { min: number; max: number }) => ratioExtent(range)?.max,
                axisLabel: { fontSize: 11, formatter: (value: number) => value.toFixed(2) },
              },
            ],
            series: growthDividendSeries(history, benchmarks),
          })
          // SVG SSR exercises ECharts' actual label layout without a browser.
          // Browser typography/visual verification is still covered by Playwright.
          chart.renderToSVGString()
          const texts = new Set(
            chart
              .getZr()
              .storage.getDisplayList()
              .map((element) => element.parent as unknown)
              .filter(
                (element): element is InstanceType<typeof graphic.Text> =>
                  element instanceof graphic.Text,
              ),
          )
          const labels = [...texts].filter((label) => /P20|P80/.test(String(label.style.text)))
          const expectedLabels = growthDividendSeries(history, benchmarks).flatMap(
            (line) => line.markLine?.data.map((reference) => reference.label.formatter) ?? [],
          )
          expect(labels.map((label) => label.style.text).sort()).toEqual(expectedLabels.sort())
          const bounds = labels.map((label) => {
            const rect = label.getBoundingRect().clone()
            rect.applyTransform(label.getComputedTransform()!)
            return rect
          })
          for (const [index, rect] of bounds.entries()) {
            expect(rect.x).toBeGreaterThanOrEqual(0)
            expect(rect.y).toBeGreaterThanOrEqual(0)
            expect(rect.x + rect.width).toBeLessThanOrEqual(width)
            expect(rect.y + rect.height).toBeLessThan(312) // Above date labels and slider.
            for (const other of bounds.slice(index + 1)) {
              expect(rect.intersect(other)).toBe(false)
            }
          }
        } finally {
          chart.dispose()
        }
      })
    }
  }
})
