import { describe, expect, it } from 'vitest'
import { dataUpdateInstant, formatDataUpdatedAt, latestDataUpdatedAt } from './dataUpdatedAt'

describe('real data update timestamps', () => {
  it('converts explicit UTC and other offsets to Beijing to the second', () => {
    expect(formatDataUpdatedAt('2026-11-18T02:03:04Z')).toBe('2026-11-18 10:03:04')
    expect(formatDataUpdatedAt('2026-11-17T18:03:04-08:00')).toBe('2026-11-18 10:03:04')
  })
  it('accepts naive time only with a confirmed Beijing source contract', () => {
    expect(dataUpdateInstant('2026-11-18T10:03:04')).toBeNull()
    expect(formatDataUpdatedAt('2026-11-18 10:03:04', true)).toBe('2026-11-18 10:03:04')
  })
  it('does not invent midnight or seconds for dates and minute-only times', () => {
    for (const value of [
      undefined,
      null,
      '',
      '2026-11-18',
      '2026-11-18T10:03',
      'nonsense',
      '2026-02-30T12:00:00Z',
      '2026-11-18T24:00:00Z',
      '2026-13-01T12:00:00Z',
    ]) {
      expect(dataUpdateInstant(value, true)).toBeNull()
      expect(formatDataUpdatedAt(value, true)).toBe('未提供可确认的秒级数据时间')
    }
  })
  it('compares instants rather than local timestamp strings', () => {
    expect(
      latestDataUpdatedAt(['2026-11-18T10:00:01+08:00', '2026-11-18T03:00:01Z', '2026-11-19']),
    ).toBe('2026-11-18T03:00:01Z')
  })
})
