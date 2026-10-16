export const formatScore = (value: number | null | undefined) =>
  value == null ? '—' : value.toFixed(2)
export const signedScore = (value: number | null | undefined) =>
  value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}`
export const displayName = (name: string) => name.replace(/(?:II|Ⅱ)$/, '').trim()
export const formatDate = (value: string | null | undefined) =>
  value ? value.replaceAll('-', '.') : '等待数据'
export const formatTimestamp = (value: string | null) =>
  value ? value.replace('T', ' ').slice(0, 16) : '—'

export function sortIndustries<T extends { momentum: number; code: string }>(items: T[]): T[] {
  return [...items].sort(
    (a, b) => b.momentum - a.momentum || (a.code < b.code ? 1 : a.code > b.code ? -1 : 0),
  )
}

const stops = [
  [24, 55, 48],
  [42, 91, 67],
  [112, 151, 91],
  [215, 184, 99],
  [250, 126, 83],
]
export function heatColor(value: number, maximum: number) {
  const ratio = maximum > 0 ? Math.min(1, Math.max(0, Math.log1p(value) / Math.log1p(maximum))) : 0
  const index = Math.min(stops.length - 2, Math.floor(ratio * (stops.length - 1)))
  const weight = ratio * (stops.length - 1) - index
  const rgb = stops[index].map((color, channel) =>
    Math.round(color + (stops[index + 1][channel] - color) * weight),
  )
  return { background: `rgb(${rgb.join(', ')})`, color: ratio > 0.48 ? '#162219' : '#e4f1e7' }
}
