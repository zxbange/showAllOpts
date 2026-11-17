import type { CashCowStock } from '../types/cashCow'
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

// Apply the display rule to retained snapshots too, without changing source data.
export function cashCowStaticHistory(
  item: Pick<CashCowStock, 'dividend_yield_history' | 'dividend_fiscal_year' | 'years'>,
) {
  const history = item.dividend_yield_history
  if (!history?.length) return history
  const latestYear = item.dividend_fiscal_year ?? Math.max(...item.years.map((row) => row.year))
  return history.map((row) => ({
    ...row,
    yield_pct:
      row.fiscal_year === latestYear
        ? row.yield_pct
        : (item.years.find((annual) => annual.year === row.fiscal_year)?.dividend_yield ?? null),
  }))
}
