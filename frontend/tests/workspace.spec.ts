import { expect, test, type Page } from '@playwright/test'

const industryNames = [
  '电力设备',
  '半导体',
  '有色金属',
  '通信设备',
  '消费电子',
  '计算机应用',
  '化学制药',
  '机械设备',
]
const industries = Array.from({ length: 127 }, (_, index) => ({
  code: `801${String(index).padStart(3, '0')}`,
  name: industryNames[index] ?? `测试行业${index + 1}`,
  momentum: index > 95 ? 0 : Number((32 - index / 3).toFixed(2)),
  rank: index + 1,
  front_stock_count: 10,
  industry_stock_count: 80,
  breadth: 0.125,
  previous_momentum: 2,
  change: 1,
}))
const snapshot = {
  industry_type: 'sw_l2',
  trade_date: '2026-10-16',
  previous_trade_date: '2026-10-15',
  updated_at: '2026-10-16T17:15:00',
  industries,
  summary: { industry_count: 127, active_count: 96, average_momentum: 8.4, max_momentum: 32 },
}

async function fixtures(page: Page) {
  await page.route('**/api/v1/modules', (route) =>
    route.fulfill({ json: { modules: [{ id: 'industry-momentum', name: '行业动量' }] } }),
  )
  await page.route('**/api/v1/industry-momentum/latest', (route) =>
    route.fulfill({ json: snapshot }),
  )
  await page.route('**/api/v1/industry-momentum/industries/*/history?*', (route) => {
    const days = new URL(route.request().url()).searchParams.get('days')
    return route.fulfill({
      json: {
        code: '801000',
        name: '电力设备',
        industry_type: 'sw_l2',
        start_date: days === '30' ? '2026-09-17' : '2025-10-17',
        end_date: '2026-10-16',
        summary: {
          latest_momentum: 32,
          latest_date: '2026-10-16',
          peak_momentum: 32,
          peak_date: '2026-10-16',
          average_momentum: 14,
          change: 24,
          observed_days: 4,
          missing_days: 1,
        },
        points: [8, 15, null, 2, 32].map((momentum, i) => ({
          date: `2026-10-${12 + i}`,
          momentum,
          rank: 1,
          front_stock_count: 10,
          industry_stock_count: 80,
        })),
      },
    })
  })
}

test('home → heatmap → industry curve, search, range and return', async ({ page }, testInfo) => {
  await fixtures(page)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /全部研究模块/ })).toBeVisible()
  await page.getByRole('link', { name: '进入动量观察', exact: true }).click()
  await expect(page.locator('.heat-cell')).toHaveCount(127)
  await expect(page.locator('.heat-cell').first()).toContainText('32.00')
  await expect(page.locator('.heat-cell').last()).toContainText('0.00')
  await expect(page.locator('.heat-cell').last()).toHaveAttribute('aria-label', /热力值 0.00/)
  await page.waitForTimeout(650)
  const last = await page.locator('.heat-cell').last().boundingBox()
  const grid = await page.locator('.heatmap-grid').boundingBox()
  expect(Math.abs(last!.x + last!.width - (grid!.x + grid!.width))).toBeLessThan(10)
  if (testInfo.project.name === 'desktop')
    await page.screenshot({
      path: testInfo.outputPath('heatmap.png'),
      fullPage: true,
      animations: 'disabled',
    })
  await page.getByRole('textbox', { name: '搜索行业名称或代码' }).fill('半导体')
  await expect(page.locator('.heat-cell')).toHaveCount(1)
  await page.getByRole('button', { name: '清空搜索' }).click()
  await page.locator('.heat-cell').first().click()
  await expect(page.getByRole('heading', { name: /电力设备.*动量趋势/ })).toBeVisible()
  expect(await page.evaluate(() => window.scrollY)).toBe(0)
  await expect(page.locator('.trend-chart canvas')).toBeVisible()
  await page.screenshot({
    path: testInfo.outputPath('industry.png'),
    fullPage: true,
    animations: 'disabled',
  })
  await expect(page.getByRole('button', { name: '近 1 年' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await page.getByRole('button', { name: '近 1 月' }).click()
  await expect(page.locator('.trend-panel')).toContainText('2026.09.17')
  await page.getByRole('link', { name: '返回行业热力图' }).click()
  await expect(page.locator('.heat-cell')).toHaveCount(127)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
})

test('database errors and empty snapshots have distinct states', async ({ page }) => {
  await page.route('**/api/v1/industry-momentum/latest', (route) =>
    route.fulfill({ status: 503, json: { detail: '暂时无法读取数据，请重试。' } }),
  )
  await page.goto('/industry-momentum')
  await expect(page.getByRole('alert')).toContainText('暂时无法读取数据')
  await page.unroute('**/api/v1/industry-momentum/latest')
  await page.route('**/api/v1/industry-momentum/latest', (route) =>
    route.fulfill({ json: { ...snapshot, trade_date: null, industries: [] } }),
  )
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '暂无行业动量数据' })).toBeVisible()
})

test('unknown industry provides a recoverable error', async ({ page }) => {
  await page.route('**/api/v1/industry-momentum/industries/*/history?*', (route) =>
    route.fulfill({ status: 404, json: { detail: '未找到该行业的数据。' } }),
  )
  await page.goto('/industry-momentum/industries/unknown')
  await expect(page.getByRole('alert')).toContainText('未找到该行业的数据')
  await expect(page.getByRole('link', { name: '返回行业热力图' })).toBeVisible()
})
