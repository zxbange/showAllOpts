import { expect, test } from '@playwright/test'

const points = [0.3, 0.5, 0.4, 0.6].map((score, i) => ({
  date: `2026-10-${String(1 + i * 5).padStart(2, '0')}`,
  score,
  micro_index: 1000 + i * 50,
  period_return: 0.05,
  micro_median: 15,
  all_median: 25,
  relative_ratio: 0.6,
  crowding_ratio: 1.5,
  amount_crowding_ratio: 0.2,
  cap_pct: 0.3,
  relative_pct: 0.6,
  crowding_pct: 0.5,
  amount_crowding_pct: 0.7,
  pe_median: -5,
  pb_median: 2,
  avg_np_growth: i === 1 ? null : -20 + i,
}))
const history = {
  start_date: '2025-10-17',
  end_date: '2026-10-16',
  latest: {
    ...points[3],
    micro_num: 1000,
    sample_step: 1,
    sample_count: 300,
    universe_count: 4000,
    profit_growth_count: 950,
    updated_at: '2026-10-16T17:00:00',
  },
  summary: {
    observed_samples: 4,
    average_score: 0.45,
    peak_score: 0.6,
    low_score: 0.3,
    score_change: 30,
    index_change_pct: 15,
  },
  points,
}

test('microcap module, metric selection, index overlay, range and financial charts', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/modules', (route) =>
    route.fulfill({ json: { modules: [{}, {}, {}] } }),
  )
  await page.route('**/api/v1/industry-momentum/latest', (route) =>
    route.fulfill({ status: 503, json: { detail: 'offline fixture' } }),
  )
  const ranges: string[] = []
  await page.route('**/api/v1/microcap-temperature/history?*', (route) => {
    const days = new URL(route.request().url()).searchParams.get('days')!
    ranges.push(days)
    return route.fulfill({
      json: {
        ...history,
        start_date: days === '0' ? '2021-01-04' : days === '30' ? '2026-09-17' : history.start_date,
      },
    })
  })
  await page.goto('/')
  await page.getByRole('link', { name: '进入微盘观察', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText('微盘温度计')
  await expect(page.locator('.stats-grid')).toContainText('60.0')
  await expect(page.locator('.trend-chart canvas')).toHaveCount(3)
  await expect(page.getByRole('img', { name: '微盘综合温度历史曲线' })).toBeVisible()
  await page.getByRole('button', { name: '相对估值', exact: true }).click()
  await expect(page.getByRole('img', { name: '微盘相对估值历史曲线' })).toBeVisible()
  const overlay = page.getByRole('checkbox', { name: '叠加微盘 1000 等权指数（右轴）' })
  await overlay.uncheck()
  await expect(overlay).not.toBeChecked()
  await overlay.check()
  await page.getByRole('button', { name: '全部', exact: true }).click()
  await expect(page.locator('.trend-panel').first()).toContainText('2021.01.04')
  await page.getByRole('button', { name: '近 1 月', exact: true }).click()
  await expect(page.locator('.trend-panel').first()).toContainText('2026.09.17')
  await expect(page.locator('.microcap-secondary')).toContainText('PE -5.00 倍')
  await expect(page.locator('.microcap-secondary')).toContainText('950 / 1000')
  await expect(page.locator('.breadcrumb')).toContainText('微盘观察')
  expect([...new Set(ranges)]).toEqual(['365', '0', '30'])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
  await page.screenshot({
    path: testInfo.outputPath('microcap.png'),
    fullPage: true,
    animations: 'disabled',
  })
})

test('microcap failure, retry and empty state', async ({ page }) => {
  await page.route('**/api/v1/microcap-temperature/history?*', (route) =>
    route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } }),
  )
  await page.goto('/microcap-temperature')
  await expect(page.getByRole('alert')).toContainText('数据库暂不可用')
  await page.unroute('**/api/v1/microcap-temperature/history?*')
  await page.route('**/api/v1/microcap-temperature/history?*', (route) =>
    route.fulfill({ json: { ...history, latest: null, points: [] } }),
  )
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '暂无微盘温度数据' })).toBeVisible()
  await expect(page.locator('.trend-chart')).toHaveCount(0)
})
