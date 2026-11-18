import { describe, it, expect } from 'vitest'
import { collectionOutcomePresentation, collectionModuleKeys } from './collectionOutcomes'
import type { CollectionOutcome } from '../api/collectionOutcomes'
const result = (outcome: CollectionOutcome['outcome']): CollectionOutcome => ({
  outcome,
  finished_at: '2026-11-18T15:00:00+08:00',
  trigger: 'manual',
})
describe('collection outcome lamps', () => {
  it('uses only a real completed success, with trigger and timestamp', () => {
    const p = collectionOutcomePresentation(result('success'))
    expect(p.tone).toBe('success')
    expect(p.label).toContain('最近一次已结束手动采集：成功')
    expect(p.label).toContain('北京时间')
  })
  it('does not let an older success override a new failed/partial result', () => {
    for (const state of ['failed', 'partial'] as const)
      expect(collectionOutcomePresentation(result(state)).tone).toBe('failed')
  })
  it('never turns empty, cancelled, interrupted, malformed or unavailable into success', () => {
    for (const state of ['unknown', 'cancelled', 'interrupted'] as const)
      expect(collectionOutcomePresentation(result(state)).tone).toBe('unknown')
    expect(collectionOutcomePresentation().tone).toBe('unknown')
    expect(collectionOutcomePresentation({ ...result('success'), finished_at: null }).tone).toBe(
      'unknown',
    )
    expect(collectionOutcomePresentation({ ...result('success'), finished_at: 'bad' }).tone).toBe(
      'unknown',
    )
    expect(collectionOutcomePresentation(result('success'), true).tone).toBe('unknown')
  })
  it('does not describe an in-flight run as a completed success', () => {
    const p = collectionOutcomePresentation({ ...result('success'), running: true })
    expect(p.tone).toBe('running')
    expect(p.label).toContain('正在采集；最近一次已结束手动采集：成功')
  })
  it('both dividend tabs share the actual collection and no group navigation key is guessed', () => {
    expect(collectionModuleKeys['dividend-value']).toBe('dividend-selection')
    expect(collectionModuleKeys['dividend-bank']).toBe('dividend-selection')
    expect(collectionModuleKeys['市场情绪']).toBeUndefined()
  })
})

it('identifies database synchronization rather than claiming source collection success', () => {
  const p = collectionOutcomePresentation({
    ...result('success'),
    kind: 'database_sync',
    trigger: 'scheduled',
  })
  expect(p.label).toContain('最近一次已结束自动数据库同步：成功')
  expect(p.label).not.toContain('采集')
})

it('distinguishes an empty completed sync from a missing completion record', () => {
  const p = collectionOutcomePresentation({ ...result('unknown'), kind: 'database_sync' })
  expect(p.tone).toBe('unknown')
  expect(p.label).toContain('最近一次已结束手动数据库同步')
  expect(p.label).toContain('未确认有效数据')
  expect(p.label).not.toContain('尚无已完成')
})
