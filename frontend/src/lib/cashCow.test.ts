import { expect, it } from 'vitest'
import { cashCowPeriod, cashCowStaticHistory, cashCowUpdateTime } from './cashCow'
it('accepts only explicit supported periods and defaults to ten years', () => {
  expect(cashCowPeriod('3')).toBe(3)
  expect(cashCowPeriod('5')).toBe(5)
  for (const value of [null, '', '10', '4', '03', '3x', '-5']) expect(cashCowPeriod(value)).toBe(10)
})

it('formats only valid successful timestamps in China time', () => {
  for (const value of [null, undefined, '', 'invalid']) expect(cashCowUpdateTime(value)).toBe('—')
  expect(cashCowUpdateTime('2026-10-31T13:00:00Z')).toBe('2026/10/31 21:00:00')
})

it('uses annual details for older static-history entries in retained snapshots', () => {
  const history = [2025, 2024, 2023].map((year) => ({ fiscal_year: year, yield_pct: 99 }))
  const item = {
    dividend_yield_history: history,
    dividend_fiscal_year: 2025,
    years: [
      { year: 2024, dividend_yield: 100 / 13 },
      { year: 2023, dividend_yield: 100 / 17 },
    ],
  } as Parameters<typeof cashCowStaticHistory>[0]
  expect(cashCowStaticHistory(item)).toEqual([
    { fiscal_year: 2025, yield_pct: 99 },
    { fiscal_year: 2024, yield_pct: 100 / 13 },
    { fiscal_year: 2023, yield_pct: 100 / 17 },
  ])
  expect(history[1].yield_pct).toBe(99)
  expect(
    cashCowStaticHistory({ dividend_yield_history: history, years: [] })?.[1].yield_pct,
  ).toBeNull()
  expect(cashCowStaticHistory({ years: [] })).toBeUndefined()
})

it('does not treat an older history entry as latest when the newest entry is absent', () => {
  const item = {
    dividend_fiscal_year: 2025,
    dividend_yield_history: [{ fiscal_year: 2024, yield_pct: 99 }],
    years: [{ year: 2024, dividend_yield: 6 }],
  } as Parameters<typeof cashCowStaticHistory>[0]
  expect(cashCowStaticHistory(item)).toEqual([{ fiscal_year: 2024, yield_pct: 6 }])
})
