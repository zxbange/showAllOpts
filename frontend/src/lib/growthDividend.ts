import type { GrowthDividendPoint } from '../types/growthDividend'

export type GrowthDividendYears = 0 | 1 | 3 | 5
export interface DateSpan {
  start: string
  end: string
}
export interface GrowthDividendSelection {
  years: GrowthDividendYears
  span?: DateSpan
  revision: number
}

export const growthDividendLines = [
  { key: 'chinext', name: '创业板指', code: '399006', color: '#3568b4', ratio: false },
  { key: 'dividend', name: '中证红利', code: '000922', color: '#a67a22', ratio: false },
  { key: 'star50', name: '科创50', code: '000688', color: '#258579', ratio: false },
  { key: 'chinext_dividend_ratio', name: '创红比', code: '', color: '#8c55a0', ratio: true },
  { key: 'star50_dividend_ratio', name: '科创红利比', code: '', color: '#c65342', ratio: true },
] as const

type GrowthDividendRatioKey = Extract<(typeof growthDividendLines)[number], { ratio: true }>['key']

export interface GrowthDividendBenchmark {
  key: GrowthDividendRatioKey
  name: string
  color: string
  sampleCount: number
  levels: Array<{ percentile: 'P20' | 'P80' | 'P20/P80'; value: number }>
}

/** Full-history percentiles are independent of the chart's visible date window. */
export function growthDividendBenchmarks(points: GrowthDividendPoint[]): GrowthDividendBenchmark[] {
  return growthDividendLines.flatMap((line) => {
    if (!line.ratio) return []
    const samples = points
      .filter((point) => line.key !== 'chinext_dividend_ratio' || point.date >= '2018-01-01')
      .map((point) => point[line.key])
      .filter(
        (value): value is number =>
          typeof value === 'number' && Number.isFinite(value) && value > 0,
      )
      .sort((a, b) => a - b)
    if (!samples.length) return []
    const quantile = (fraction: number) => {
      const position = (samples.length - 1) * fraction
      const lower = Math.floor(position)
      return samples[lower] + (samples[Math.ceil(position)] - samples[lower]) * (position - lower)
    }
    const p20 = quantile(0.2)
    const p80 = quantile(0.8)
    return [
      {
        key: line.key,
        name: line.name,
        color: line.color,
        sampleCount: samples.length,
        levels:
          p20 === p80
            ? [{ percentile: 'P20/P80' as const, value: p20 }]
            : [
                { percentile: 'P20' as const, value: p20 },
                { percentile: 'P80' as const, value: p80 },
              ],
      },
    ]
  })
}

/** Keep full-history references inside the visible ratio axis. */
export function growthDividendRatioExtent(
  range: { min: number; max: number },
  referenceValues: number[],
) {
  const values = [range.min, range.max, ...referenceValues].filter(Number.isFinite)
  if (!values.length) return undefined
  const min = Math.min(...values)
  const max = Math.max(...values)
  // A little breathing room for the outer references; labels live outside the plot.
  const padding = (max - min || Math.abs(max) || 1) * 0.08
  return { min: Math.max(0, min - padding), max: max + padding }
}

/** Calendar-year subtraction clamps leap day instead of drifting into March. */
export function calendarYearsAgo(day: string, years: number): string {
  const [year, month, date] = day.split('-').map(Number)
  const target = year - years
  const lastDay = new Date(Date.UTC(target, month, 0)).getUTCDate()
  return `${target}-${String(month).padStart(2, '0')}-${String(Math.min(date, lastDay)).padStart(2, '0')}`
}

/** A window only changes the view; every historical point stays in the chart. */
export function growthDividendSpan(
  points: GrowthDividendPoint[],
  years: GrowthDividendYears,
  custom?: DateSpan,
): DateSpan | null {
  const first = points[0]?.date
  const reversed = [...points].reverse()
  // A calendar can already contain today's still-missing row. Preset year
  // windows end on a real observation; all/custom windows can inspect gaps.
  const latest =
    years && !custom
      ? reversed.find(
          (point) => point.chinext != null || point.dividend != null || point.star50 != null,
        )?.date
      : points.at(-1)?.date
  if (!first || !latest) return null
  const cutoff = custom?.start ?? (years ? calendarYearsAgo(latest, years) : first)
  const until = custom?.end ?? latest
  if (cutoff > until || cutoff > latest || until < first) return null
  const start = points.find((point) => point.date >= cutoff)?.date ?? latest
  const end = reversed.find((point) => point.date <= until)?.date ?? first
  return start <= end ? { start, end } : null
}

export function growthDividendSeries(
  points: GrowthDividendPoint[],
  benchmarks = growthDividendBenchmarks(points),
) {
  return growthDividendLines.map((line) => ({
    id: line.key,
    name: line.name,
    type: 'line' as const,
    yAxisIndex: line.ratio ? 1 : 0,
    data: points.map((point) => point[line.key]),
    smooth: false,
    showSymbol: points.length < 3,
    symbolSize: 4,
    connectNulls: false,
    lineStyle: {
      color: line.color,
      type: line.ratio ? ('dashed' as const) : ('solid' as const),
      width: line.ratio ? 2.1 : 1.8,
    },
    itemStyle: { color: line.color },
    markLine: line.ratio
      ? {
          silent: true,
          animation: false,
          symbol: 'none',
          z: 2,
          label: { show: false },
          lineStyle: { color: line.color, type: 'dashed' as const, width: 1, opacity: 0.5 },
          data: (benchmarks.find((benchmark) => benchmark.key === line.key)?.levels ?? []).map(
            (level) => ({
              name: `${line.name} ${level.percentile}`,
              yAxis: level.value,
            }),
          ),
        }
      : undefined,
    emphasis: { focus: 'series' as const },
    z: line.ratio ? 4 : 3,
  }))
}

export const growthDividendValue = (value: number | null | undefined, ratio = false) =>
  value == null || !Number.isFinite(value)
    ? '—'
    : value.toLocaleString('zh-CN', {
        minimumFractionDigits: ratio ? 4 : 2,
        maximumFractionDigits: ratio ? 4 : 2,
      })
