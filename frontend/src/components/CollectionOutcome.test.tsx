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

it.each([
  ['success', false, 'success'],
  ['failed', false, 'failed'],
  ['partial', false, 'failed'],
  ['running', false, 'running'],
  ['missing', false, 'unknown'],
  ['success', true, 'unknown'],
] as const)(
  'groups the latest social outcomes into one light: %s, unavailable=%s',
  (outcome, unavailable, tone) => {
    const item = {
      outcome: 'success' as const,
      finished_at: '2026-11-18T12:00:00+08:00',
      trigger: 'scheduled' as const,
    }
    const html = renderToStaticMarkup(
      <CollectionOutcomeContext.Provider
        value={{
          data: {
            items: {
              'social-weibo': item,
              ...(outcome === 'missing'
                ? {}
                : {
                    'social-xueqiu':
                      outcome === 'running' ? { ...item, running: true } : { ...item, outcome },
                  }),
            },
          },
          unavailable,
        }}
      >
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
                collectionKey: 'social-feed',
                collectionMembers: [
                  { key: 'social-weibo', name: '微博' },
                  { key: 'social-xueqiu', name: '雪球' },
                ],
              },
              { id: 'settings', name: '平台登录', description: '登录', icon: Coins },
            ]}
          />
        </MemoryRouter>
      </CollectionOutcomeContext.Provider>,
    )
    expect(html.match(/data-collection-key=/g)).toHaveLength(1)
    expect(html).toContain(
      `<strong>最新动态<span class="collection-outcome collection-outcome--${tone}"`,
    )
    expect(html).toContain('data-collection-key="social-feed"')
    expect(html).toMatch(/aria-label="微博：[^"]+；雪球：[^"]+"/)
    expect(html).toContain('<strong>平台登录</strong>')
  },
)
