import { expect, test } from '@playwright/test'

const history = {
  top_pct: 0.05,
  start_date: '2025-10-17',
  end_date: '2026-10-16',
  latest: {
    date: '2026-10-16',
    ratio: 0.4,
    stock_count: 5000,
    top_stock_count: 250,
    top_money: 400000000000,
    total_money: 1000000000000,
    updated_at: '2026-10-16T17:00:00',
  },
  summary: {
    observed_days: 4,
    average_ratio: 0.35,
    peak_ratio: 0.4,
    low_ratio: 0.3,
    change_pp: 10,
  },
  points: [0.3, 0.32, 0.38, 0.4].map((ratio, i) => ({
    date: `2026-10-${13 + i}`,
    ratio,
    smoothed_ratio: ratio - 0.01,
  })),
}

test('home to crowding, chart, ranges and smoothing on desktop/mobile', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/modules', (route) =>
    route.fulfill({ json: { modules: [{ id: 'industry-momentum' }, { id: 'market-crowding' }] } }),
  )
  await page.route('**/api/v1/industry-momentum/latest', (route) =>
    route.fulfill({ status: 503, json: { detail: 'offline fixture' } }),
  )
  const ranges: string[] = []
  await page.route('**/api/v1/market-crowding/history?*', (route) => {
    const days = new URL(route.request().url()).searchParams.get('days')!
    ranges.push(days)
    return route.fulfill({
      json: {
        ...history,
        start_date: days === '0' ? '2020-01-02' : days === '30' ? '2026-09-17' : history.start_date,
      },
    })
  })
  await page.goto('/')
  await page.locator('.crowding-module').click()
  await expect(page.getByRole('heading', { name: /抱团拥挤度.*成交额集中度/ })).toBeVisible()
  await expect(page.locator('.trend-chart canvas')).toBeVisible()
  await expect(page.locator('.stats-grid')).toContainText('40.00%')
  await page.getByRole('button', { name: '全部', exact: true }).click()
  await expect(page.locator('.trend-panel')).toContainText('2020.01.02')
  await page.getByRole('button', { name: '近 1 月', exact: true }).click()
  await expect(page.locator('.trend-panel')).toContainText('2026.09.17')
  const smoothing = page.getByRole('checkbox', { name: '显示 EMA 10 平滑参考线' })
  await smoothing.uncheck()
  await expect(smoothing).not.toBeChecked()
  await smoothing.check()
  await expect(smoothing).toBeChecked()
  await expect(page.locator('.breadcrumb')).toContainText('抱团拥挤度')
  // React StrictMode may start/abort the initial request twice in development.
  expect([...new Set(ranges)]).toEqual(['365', '0', '30'])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
})

test('crowding failure, retry, and empty state', async ({ page }) => {
  await page.route('**/api/v1/market-crowding/history?*', (route) =>
    route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } }),
  )
  await page.goto('/market-crowding')
  await expect(page.getByRole('alert')).toContainText('数据库暂不可用')
  await page.unroute('**/api/v1/market-crowding/history?*')
  await page.route('**/api/v1/market-crowding/history?*', (route) =>
    route.fulfill({
      json: { ...history, latest: null, points: [], summary: { observed_days: 0 } },
    }),
  )
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '暂无拥挤度数据' })).toBeVisible()
  await expect(page.locator('.trend-chart')).toHaveCount(0)
})
