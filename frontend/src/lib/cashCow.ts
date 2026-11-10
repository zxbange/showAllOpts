export type CashCowPeriod = 3 | 5 | 10
export function cashCowPeriod(value: string | null): CashCowPeriod {
  return value === '3' ? 3 : value === '5' ? 5 : 10
}

export function cashCowUpdateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
