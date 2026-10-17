import type { ETFHistory } from '../types/etf'

export const etfColors = ['#c44837', '#337fa5', '#ad7c14', '#217b70', '#8d63a6']
export const billionShares = (value: string | null) =>
  value == null ? '—' : (Number(value) / 1e8).toFixed(2)
export const signedPercent = (value: number | null) =>
  value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`

export function chartSeries(data: ETFHistory, relative: boolean, selected: string | null) {
  const dates = [
    ...new Set(data.funds.flatMap((fund) => fund.points.map((point) => point.date))),
  ].sort()
  const series = data.funds.flatMap((fund, index) => {
    if (selected && fund.code !== selected) return []
    const points = new Map(fund.points.map((point) => [point.date, point]))
    return [
      {
        name: `${fund.name} ${fund.code.split('.')[0]}`,
        color: etfColors[index % etfColors.length],
        values: dates.map((date) => {
          const point = points.get(date)
          return point ? (relative ? point.change_pct : Number(point.shares) / 1e8) : null
        }),
      },
    ]
  })
  return { dates, series }
}
