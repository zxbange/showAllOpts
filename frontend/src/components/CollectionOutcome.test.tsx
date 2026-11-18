import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { Coins } from 'lucide-react'
import { expect, it } from 'vitest'
import { CollectionOutcomeContext, CollectionOutcomeLight } from './CollectionOutcome'
import { ModuleSwitch } from './ModuleSwitch'

it('renders an accessible non-color-only lamp directly after the unchanged tab label', () => {
  const html = renderToStaticMarkup(
    <CollectionOutcomeContext.Provider
      value={{
        data: {
          items: {
            'dividend-selection': {
              outcome: 'failed',
              finished_at: '2026-11-18T12:00:00+08:00',
              trigger: 'manual',
            },
          },
        },
        unavailable: false,
      }}
    >
      <MemoryRouter>
        <ModuleSwitch
          label="业绩选股"
          items={[
            {
              id: 'dividend-bank',
              name: '银行',
              description: '银行选股',
              icon: Coins,
              href: '/dividend-selection?method=bank',
            },
          ]}
        />
      </MemoryRouter>
    </CollectionOutcomeContext.Provider>,
  )
  expect(html).toContain('aria-label="银行"')
  expect(html).toContain('<strong>银行<span class="collection-outcome collection-outcome--failed"')
  expect(html).toContain('role="img"')
  expect(html).toContain('aria-label="最近一次已结束手动采集：失败；')
  expect(html).toContain('data-collection-key="dividend-selection" data-symbol="!"></span>')
  expect(html.replace(/<[^>]+>/g, '')).toBe('银行')
})

it('renders neutral when no completion exists, or the status endpoint is unavailable', () => {
  expect(renderToStaticMarkup(<CollectionOutcomeLight moduleKey="cash-cow" />)).toContain(
    '尚无已完成采集记录',
  )
  const html = renderToStaticMarkup(
    <CollectionOutcomeContext.Provider
      value={{
        data: {
          items: {
            'cash-cow': {
              outcome: 'success',
              finished_at: '2026-11-18T12:00:00+08:00',
              trigger: 'scheduled',
            },
          },
        },
        unavailable: true,
      }}
    >
      <CollectionOutcomeLight moduleKey="cash-cow" />
    </CollectionOutcomeContext.Provider>,
  )
  expect(html).toContain('collection-outcome--unknown')
  expect(html).not.toContain('collection-outcome--success')
})

it('keeps two social platforms distinct and omits lights from settings tabs', () => {
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <ModuleSwitch
        tabs
        label="博主监控"
        items={[
          {
            id: 'feed',
            name: '最新动态',
            description: '动态',
            icon: Coins,
            collectionKeys: [
              { key: 'social-weibo', name: '微博' },
              { key: 'social-xueqiu', name: '雪球' },
            ],
          },
          { id: 'settings', name: '平台登录', description: '登录', icon: Coins },
        ]}
      />
    </MemoryRouter>,
  )
  expect(html).toContain('aria-label="微博：尚无已完成采集记录"')
  expect(html).toContain('aria-label="雪球：尚无已完成采集记录"')
  expect(html).toContain('<strong>平台登录</strong>')
})
