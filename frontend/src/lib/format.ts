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
  [250, 241, 223],
  [244, 211, 159],
  [237, 156, 108],
  [219, 98, 75],
  [170, 48, 47],
]
export function heatColor(value: number, maximum: number) {
  const ratio = maximum > 0 ? Math.min(1, Math.max(0, Math.log1p(value) / Math.log1p(maximum))) : 0
  const index = Math.min(stops.length - 2, Math.floor(ratio * (stops.length - 1)))
  const weight = ratio * (stops.length - 1) - index
  const rgb = stops[index].map((color, channel) =>
    Math.round(color + (stops[index + 1][channel] - color) * weight),
  )
  // Choose readable text from actual luminance, independent of the score distribution.
  const linear = rgb.map((channel) => {
    const c = channel / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  const luminance = linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
  return { background: `rgb(${rgb.join(', ')})`, color: luminance > 0.179 ? '#000000' : '#ffffff' }
}
