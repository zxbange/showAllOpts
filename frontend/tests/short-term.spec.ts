import { expect, test } from '@playwright/test'
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/short-term/validation/jobs', (r) => r.fulfill({ json: { jobs: [] } }))
})
const bars = Array.from({ length: 30 }, (_, i) => ({
  date: `2026-09-${String(i + 1).padStart(2, '0')}`,
  open: 10 + i * 0.1,
  close: 10.2 + i * 0.1,
  low: 9.9 + i * 0.1,
  high: 10.4 + i * 0.1,
  volume: 1000000 + i * 10000,
  ma: {
    '5': 10 + i * 0.1,
    '10': 9.9 + i * 0.1,
    '20': 9.8 + i * 0.1,
    '30': 9.7 + i * 0.1,
    '60': 9.5 + i * 0.1,
  },
}))

test('short term tabs, dates, candles and pagination', async ({ page }, info) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/short-term/latest?*', (r) => {
    const params = new URL(r.request().url()).searchParams
    const method = params.get('method') || 'bukou'
    const offset = Number(params.get('offset'))
    return r.fulfill({
      json: {
        trade_date: params.get('day') || '2026-09-30',
        dates: ['2026-09-30', '2026-09-29', '2026-09-28'],
        method,
        counts: { bukou: 7, redhover: 0, xuli: 1, oversold: 1 },
        meta: { universe_count: 5100 },
        total: method === 'redhover' ? 0 : method === 'bukou' ? 7 : 1,
        offset,
        limit: 6,
        items:
          method === 'redhover'
            ? []
            : [
                {
                  code: '600001.XSHG',
                  name: offset ? '下一页股票' : '形态示例',
                  methods: [method],
                  industry: '测试行业',
                  close: 13.1,
                  pct_change: 3.5,
                  pe: 12,
                  pb: 2,
                  market_cap: 60,
                  volume: 1000000,
                  bars,
                },
              ],
      },
    })
  })
  await page.goto('/short-term')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('短线选股')
  await expect(page.getByRole('img', { name: /形态示例近30个交易日/ })).toBeVisible()
  await page.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(page.getByRole('heading', { name: '下一页股票 600001.XSHG' })).toBeVisible()
  await page.getByRole('link', { name: /红悬停龙/ }).click()
  await expect(page.getByRole('heading', { name: '本日暂无符合条件的股票' })).toBeVisible()
  await page.getByRole('link', { name: /蓄力龙/ }).click()
  await page.getByLabel('短线选股日期').selectOption('2026-09-29')
  await expect(page.locator('.short-summary')).toContainText('2026-09-29')
  await expect(page.getByRole('img', { name: /形态示例近30个交易日/ })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.getByRole('link', { name: /超跌后止跌反弹/ }).click()
  await expect(page.locator('.short-summary').first()).toContainText('后复权')
  await expect(page.getByRole('img', { name: /形态示例近30个交易日/ })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
  await page.screenshot({ path: info.outputPath('short-term.png'), fullPage: true })
})

test('short term empty and failed source states', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({
      json: {
        trade_date: null,
        dates: [],
        method: 'bukou',
        counts: {},
        meta: {},
        total: 0,
        items: [],
      },
    }),
  )
  await page.goto('/short-term')
  await expect(page.getByRole('heading', { name: '尚未采集短线选股' })).toBeVisible()
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({ status: 503, json: { detail: '本地快照尚未准备好' } }),
  )
  await page.getByRole('button', { name: '刷新短线选股' }).click()
  await expect(page.getByText('本地快照尚未准备好')).toBeVisible()
})

test('old snapshots distinguish uncomputed rebound from zero candidates', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({
      json: {
        trade_date: '2026-09-30',
        dates: ['2026-09-30'],
        method: 'oversold',
        counts: { bukou: 0 },
        meta: {},
        total: 0,
        items: [],
      },
    }),
  )
  await page.goto('/short-term')
  await page.getByRole('link', { name: /超跌后止跌反弹/ }).click()
  await expect(page.getByRole('heading', { name: '此快照尚未计算超跌反弹' })).toBeVisible()
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({
      json: {
        trade_date: '2026-09-30',
        dates: ['2026-09-30'],
        method: 'oversold',
        counts: { oversold: 0 },
        meta: {},
        total: 0,
        items: [],
      },
    }),
  )
  await page.getByRole('button', { name: '刷新短线选股' }).click()
  await expect(page.getByRole('heading', { name: '本日暂无符合条件的股票' })).toBeVisible()
})
