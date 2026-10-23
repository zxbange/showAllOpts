import { expect, test } from '@playwright/test'

const stock = {
  report_period: '2026-09-30',
  code: '600001.XSHG',
  name: '示例制造',
  industry_code: '801001',
  industry_name: '专用设备',
  industry_level: 'sw_l3',
  signal_date: '2026-10-15',
  announcement_date: '2026-10-15',
  signal_source: 'formal',
  profit_basis: '单季度',
  profit_yoy: 35,
  net_profit: 120000000,
  open_gap_pct: 4,
  intraday_pct: 1,
  market_cap: 123.45,
  circulating_market_cap: 80,
  pe_ttm: 18,
  pb: 2.5,
  valuation_date: '2026-10-16',
}
const detail = {
  ...stock,
  events: [
    {
      code: stock.code,
      kind: 'formal',
      publication_date: '2026-10-15',
      report_period: '2026-09-30',
      net_profit: stock.net_profit,
      profit_yoy: 35,
      profit_yoy_max: null,
      profit_basis: '单季度',
      eligible: true,
      reason: null,
      reason_field: null,
      raw: { net_profit: stock.net_profit, report_period: '2026-09-30' },
      signal: { date: stock.signal_date, open_gap_pct: 4, intraday_pct: 1 },
    },
  ],
  prices: ['2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16'].map((date, i) => ({
    date,
    open: 10 + i * 0.2,
    close: 10.1 + i * 0.2,
    low: 9.8 + i * 0.2,
    high: 10.3 + i * 0.2,
  })),
}
const overview = {
  target_period: '2026-09-30',
  periods: ['2026-09-30', '2026-06-30'],
  run: {
    report_period: '2026-09-30',
    as_of_date: '2026-10-16',
    updated_at: '2026-10-17T17:00:00',
    universe_count: 5400,
    matched_count: 31,
    rule_version: 'announcement-open-v1',
  },
  industries: [{ code: '801001', name: '专用设备' }],
  total: 31,
  page: 1,
  page_size: 30,
  items: [stock],
}

test('earnings entry, filters, pagination, detail and mobile layout', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  const requests: URL[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.route('**/api/v1/earnings-gap/**', (route) => {
    const url = new URL(route.request().url())
    requests.push(url)
    if (url.pathname.endsWith(stock.code)) return route.fulfill({ json: detail })
    return route.fulfill({ json: { ...overview, page: Number(url.searchParams.get('page') ?? 1) } })
  })
  await page.goto('/earnings-gap')
  await expect(page.locator('h1 .earnings-title-quarter')).toHaveText('2026 年 Q3')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('净利润断层')
  await expect(page.getByRole('link', { name: stock.name })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '断层原因分析' })).toHaveCount(0)
  await expect(page.getByRole('columnheader')).toHaveCount(6)
  await page.getByLabel('细分行业', { exact: true }).selectOption('801001')
  await expect.poll(() => requests.at(-1)?.searchParams.get('industry')).toBe('801001')
  await page.getByLabel('排序', { exact: true }).selectOption('profit_yoy')
  await expect.poll(() => requests.at(-1)?.searchParams.get('sort')).toBe('profit_yoy')
  await page.getByLabel('股票搜索', { exact: true }).fill('示例')
  await page.getByRole('button', { name: '搜索股票', exact: true }).click()
  await expect.poll(() => requests.at(-1)?.searchParams.get('search')).toBe('示例')
  await page.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(page.getByText('第 2 / 2 页')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: testInfo.outputPath('earnings-list.png'), fullPage: true })
  await page.getByRole('link', { name: stock.name }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText(stock.name)
  await expect(page.getByRole('heading', { name: '断层原因分析' })).toHaveCount(0)
  await expect(page.getByRole('img', { name: '信号附近日线走势' }).locator('canvas')).toBeVisible()
  await expect(page.getByText('未取得该期披露数据')).toHaveCount(2)
  await page.getByText('查看数据源原始字段').click()
  await expect(page.locator('.earnings-raw pre')).toContainText('120000000')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: testInfo.outputPath('earnings-detail.png'), fullPage: true })
  expect(errors).toEqual([])
})

test('earnings no collection, zero results, filtered empty and retry', async ({ page }) => {
  let mode = 'uncollected'
  await page.route('**/api/v1/earnings-gap/**', (route) => {
    if (mode === 'error')
      return route.fulfill({ status: 503, json: { detail: '暂时无法读取数据' } })
    return route.fulfill({
      json: {
        ...overview,
        total: 0,
        items: [],
        run:
          mode === 'uncollected'
            ? null
            : { ...overview.run, matched_count: mode === 'filtered' ? 31 : 0 },
      },
    })
  })
  await page.goto('/earnings-gap')
  await expect(page.getByText('本季度尚未采集')).toBeVisible()
  mode = 'zero'
  await page.getByRole('button', { name: '刷新净利润断层' }).click()
  await expect(page.getByText('本季度暂未出现符合条件的股票')).toBeVisible()
  mode = 'filtered'
  await page.getByRole('button', { name: '刷新净利润断层' }).click()
  await expect(page.getByText('没有匹配的股票')).toBeVisible()
  mode = 'error'
  await page.getByRole('button', { name: '刷新净利润断层' }).click()
  await expect(page.getByRole('alert')).toContainText('暂时无法读取数据')
  mode = 'zero'
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByText('本季度暂未出现符合条件的股票')).toBeVisible()
})
