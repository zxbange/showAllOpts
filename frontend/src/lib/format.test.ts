import { describe, expect, it } from 'vitest'
import { formatScore, heatColor, sortIndustries } from './format'

describe('heatmap data', () => {
  it('sorts descending without mutating input, with deterministic code ties', () => {
    const rows = [
      { code: 'A', momentum: 0 },
      { code: 'B', momentum: 2 },
      { code: 'C', momentum: 2 },
    ]
    expect(sortIndustries(rows).map((r) => r.code)).toEqual(['C', 'B', 'A'])
    expect(rows[0].code).toBe('A')
  })
  it('distinguishes a true zero from missing data', () => {
    expect(formatScore(0)).toBe('0.00')
    expect(formatScore(null)).toBe('—')
  })
  it('has finite deterministic colors for all-zero and equal-value datasets', () => {
    expect(heatColor(0, 0).background).toBe('rgb(24, 55, 48)')
    expect(heatColor(5, 5).background).toBe('rgb(250, 126, 83)')
    expect(heatColor(0, 10).background).not.toContain('NaN')
  })
})
