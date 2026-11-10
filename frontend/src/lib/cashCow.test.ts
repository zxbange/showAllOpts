import { expect, it } from 'vitest'
import { cashCowPeriod, cashCowUpdateTime } from './cashCow'
it('accepts only explicit supported periods and defaults to ten years', () => {
  expect(cashCowPeriod('3')).toBe(3)
  expect(cashCowPeriod('5')).toBe(5)
  for (const value of [null, '', '10', '4', '03', '3x', '-5']) expect(cashCowPeriod(value)).toBe(10)
})

it('formats only valid successful timestamps in China time', () => {
  for (const value of [null, undefined, '', 'invalid']) expect(cashCowUpdateTime(value)).toBe('—')
  expect(cashCowUpdateTime('2026-10-31T13:00:00Z')).toBe('2026/10/31 21:00:00')
})
