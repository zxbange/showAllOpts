import { expect, test } from '@playwright/test'
const stock = { code: '600001.XSHG', name: '示例科技', industry: '半导体' }
const fixture = {
  stock,
  reports: [
    {
      date: '2026-06-30',
      payload: {
        categories: {
          public_fund: { market_value: 1e9, holder_count: 30 },
          social: { market_value: 2e8, holder_count: 2 },
        },
      },
    },
    {
      date: '2026-09-30',
      payload: {
        categories: {
          public_fund: { market_value: 1.2e9, holder_count: 35 },
          social: { market_value: 3e8, holder_count: 3 },
        },
      },
    },
  ],
  holders: [
    { date: '2026-06-30', pub_date: '2026-08-20', payload: { a_share_holders: 30000 } },
    { date: '2026-09-30', pub_date: '2026-10-20', payload: { a_share_holders: 27000 } },
  ],
  holder_change: -3000,
  holder_change_pct: -10,
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/institution-ownership/stocks?*', (r) =>
    r.fulfill({ json: { stocks: [stock] } }),
  )
  await page.route('**/api/v1/institution-ownership/stocks/600001.XSHG', (r) =>
    r.fulfill({ json: fixture }),
  )
  await page.route('**/api/v1/institution-ownership/social-entries?*', (r) =>
    r.fulfill({
      json: {
        period: '2026-09-30',
        periods: ['2026-09-30', '2026-06-30'],
        comparable_count: 100,
        unknown_count: 25,
        collected_count: 125,
        items: [
          {
            ...stock,
            new_combinations: [{ id: 'a', name: '全国社保基金一零一组合', ratio: 0.5 }],
            new_shares: 1e6,
            new_market_value: 2e7,
            pub_date: '2026-10-20',
          },
        ],
      },
    }),
  )
})
test('ownership search, disclosure charts and social drilldown', async ({ page }, info) => {
  await page.goto('/institution-ownership')
  await expect(page.locator('.ownership-results button')).toHaveCount(0)
  await page.getByLabel('搜索股票').fill('600001')
  await page.getByRole('button', { name: '示例科技 600001.XSHG' }).click()
  await expect(page.getByRole('heading', { name: '股东户数', exact: true })).toBeVisible()
  await expect(page.getByText('27,000')).toBeVisible()
  await expect(page.getByRole('img', { name: '公募基金持仓趋势' })).toBeVisible()
  await page.getByRole('button', { name: '社保新进', exact: true }).click()
  await expect(page.getByText('全国社保基金一零一组合')).toBeVisible()
  await page.getByLabel('社保筛选口径').selectOption('combination')
  await expect(page.getByText('全国社保基金一零一组合')).toBeVisible()
  await page.getByRole('button', { name: '示例科技', exact: true }).click()
  await expect(page.getByRole('heading', { name: '股东户数', exact: true })).toBeVisible()
  const width = await page.evaluate(() => ({
    body: document.documentElement.scrollWidth,
    window: innerWidth,
  }))
  expect(width.body).toBeLessThanOrEqual(width.window + 1)
  await page.screenshot({ path: `../.runtime/ownership-${info.project.name}.png`, fullPage: true })
})
test('ownership error permits retry', async ({ page }) => {
  await page.route('**/api/v1/institution-ownership/stocks?*', (r) =>
    r.fulfill({ status: 503, json: { detail: '本地数据尚未准备好' } }),
  )
  await page.goto('/institution-ownership')
  await expect(page.locator('.ownership-results button')).toHaveCount(0)
  await page.getByLabel('搜索股票').fill('600001')
  await expect(page.getByText('本地数据尚未准备好')).toBeVisible()
  await expect(page.getByRole('button', { name: '重新加载' })).toBeVisible()
})
