import { expect, test } from '@playwright/test'
const reports = Object.fromEntries(
  [
    ['annual', '12-31'],
    ['q1', '03-31'],
    ['q2', '06-30'],
    ['q3', '09-30'],
  ].map(([key, md]) => [
    key,
    [2023, 2024, 2025].map((year, i) => ({
      report_period: `${year}-${md}`,
      publication_date: `${year + 1}-02-01`,
      net_profit: [1e8, 1.4e8, 2.1e8][i],
      eps: [0.5, 0.7, 1.05][i],
      profit_growth: i === 0 ? null : i === 1 ? 40 : 50,
      eps_growth: i === 0 ? null : i === 1 ? 40 : 50,
    })),
  ]),
)
const fixture = {
  snapshot: {
    as_of_date: '2026-10-20',
    valuation_date: '2026-10-19',
    updated_at: '2026-10-20T18:00:00',
    universe_count: 5100,
    evaluated_count: 4000,
    matched_count: 2,
    rule_version: 'v1',
  },
  items: [
    {
      code: '600001.XSHG',
      name: '示例科技',
      rank: 1,
      industry_name: '半导体',
      pe_ttm: 28.5,
      pb: 3.6,
      market_cap: 230.5,
      annual_profit_growth: 50,
      reports,
    },
    {
      code: '000002.XSHE',
      name: '示例制造',
      rank: 2,
      industry_name: '专用设备',
      pe_ttm: null,
      pb: 2.1,
      market_cap: 80,
      annual_profit_growth: 40,
      reports,
    },
  ],
}

test('growth evidence, charts, search and responsive layout', async ({ page }, info) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/high-growth/latest', (r) => r.fulfill({ json: fixture }))
  await page.goto('/high-growth')
  await expect(page.locator('.growth-stock')).toHaveCount(2)
  await expect(page.getByRole('img', { name: '年报最近三期归母净利润', exact: true })).toHaveCount(
    2,
  )
  await expect(page.locator('.growth-stock').first()).toContainText('28.50')
  await page.locator('.growth-evidence summary').first().click()
  await expect(page.locator('.growth-evidence').first().locator('tbody tr')).toHaveCount(12)
  await expect(page.locator('.growth-evidence').first()).toContainText('基本 EPS / 元')
  await page.locator('.growth-evidence summary').first().click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({
    path: info.outputPath('growth.png'),
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('textbox', { name: '搜索高增长股票' }).fill('专用设备')
  await expect(page.locator('.growth-stock')).toHaveCount(1)
  await expect(page.locator('.growth-stock')).toContainText('示例制造')
  await page.getByRole('textbox', { name: '搜索高增长股票' }).fill('不存在')
  await expect(page.getByText('没有匹配的股票', { exact: true })).toBeVisible()
})

test('growth empty and failure states', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  let mode = 0
  await page.route('**/api/v1/high-growth/latest', (r) =>
    mode === 2
      ? r.fulfill({ status: 503, json: { detail: '暂时无法读取数据' } })
      : r.fulfill({
          json:
            mode === 0
              ? { snapshot: null, items: [] }
              : { snapshot: { ...fixture.snapshot, matched_count: 0 }, items: [] },
        }),
  )
  await page.goto('/high-growth')
  await expect(page.getByText('暂无高增长选股结果', { exact: true })).toBeVisible()
  mode = 1
  await page.getByRole('button', { name: '刷新高增长', exact: true }).click()
  await expect(page.getByText('本次暂无符合条件的股票', { exact: true })).toBeVisible()
  mode = 2
  await page.getByRole('button', { name: '刷新高增长', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('暂时无法读取数据')
})
