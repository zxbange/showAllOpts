import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
const source = (name: string) => readFileSync(new URL(`../${name}.tsx`, import.meta.url), 'utf8')
describe('compact collection controls replace obsolete footer cards', () => {
  it.each([
    ['pages/GrowthDividendPage', 'growth-dividend-update', 'act'],
    ['pages/MarginPage', 'margin-update panel', 'act'],
    ['pages/ValuationRulerPage', 'panel valuation-update', 'act'],
    ['components/MarketCollectionPanel', 'panel market-collection-panel', 'run'],
  ])('%s retains portal actions without the old card', (file, oldClass, handler) => {
    const text = source(file)
    expect(text).not.toContain(`className="${oldClass}"`)
    expect(text).toContain('<PageHeaderActions>')
    expect(text).toContain(`void ${handler}(`)
    expect(text).toContain('disabled=')
    expect(text).toContain('role="alert"')
  })
  it('retains cancel handlers and operation guards', () => {
    for (const file of ['GrowthDividendPage', 'MarginPage']) {
      const text = source(`pages/${file}`)
      expect(text).toContain("act('cancel')")
      expect(text).toContain('取消采集')
      expect(text).toContain('busy')
    }
    expect(source('components/MarketCollectionPanel')).toContain('lock.current')
    expect(source('components/MarketCollectionPanel')).toContain(
      'requestId.current ??= crypto.randomUUID()',
    )
  })
  it('removes the redundant growth comparison description', () => {
    expect(source('pages/GrowthDividendPage')).not.toContain(
      '对照创业板、科创50与中证红利，观察成长与红利的相对强弱。',
    )
  })
  it('keeps valuation status polling automatic with no manual reread control', () => {
    const valuation = source('pages/ValuationRulerPage')
    expect(valuation).not.toContain('重新读取采集状态')
    expect(valuation).not.toContain('onClick={() => void readStatus()}')
    expect(valuation).toContain('useCollectionTask(valuationCollectionTask, session, admin)')
    expect(valuation).toContain('disabled={disabled || running}')
    expect(valuation).toContain('采集中…')
    expect(valuation).not.toContain('取消采集')
  })
  it('describes preserved growth data honestly on failure', () => {
    expect(source('pages/GrowthDividendPage')).toContain(
      "data?.points.length ? ' 已保留现有数据。' : ''",
    )
  })
})
