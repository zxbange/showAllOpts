import { expect, test } from '@playwright/test'

const points = [0.19, 0.2, 0.8, 0.81, 0.196].map((ratio, i) => ({
  date: `2026-10-${19 + i}`,
  ratio,
  above_count: Math.round(ratio * 5000),
  eligible_count: 5000,
  universe_count: 5250,
  state: ratio < 0.2 ? 'cold' : ratio > 0.8 ? 'hot' : 'normal',
}))
const history = {
  start_date: '2023-10-24',
  end_date: '2026-10-23',
  latest: { ...points[4], updated_at: '2026-10-23T18:00:00' },
  points,
  summary: {
    observed_days: 5,
    cold_days: 2,
    hot_days: 1,
    low_ratio: 0.19,
    peak_ratio: 0.81,
    average_ratio: 0.4392,
    change_pp: 0.6,
  },
}

test('sentiment navigation, three-year default, chart and range changes', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const ranges: string[] = []
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.route('**/api/v1/market-sentiment/history?*', (route) => {
    const days = new URL(route.request().url()).searchParams.get('days')!
    ranges.push(days)
    return route.fulfill({
      json: { ...history, start_date: days === '30' ? '2026-09-24' : history.start_date },
    })
  })
  await page.goto('/market-crowding')
  const tabs = page.getByRole('navigation', { name: '市场情绪模块切换' })
  await tabs.getByRole('link', { name: '市场情绪跟踪', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText('市场情绪')
  await expect(page.getByRole('button', { name: '近3年', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await expect(page.locator('.stats-grid')).toContainText('19.60%')
  await expect(page.locator('.sentiment-state')).toHaveText('冰点')
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
  await expect(page.locator('.method-card')).toContainText('当日停牌')
  await expect(page.locator('.panel-heading')).toContainText('2023-10-24')
  await page.screenshot({
    path: testInfo.outputPath('sentiment.png'),
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('button', { name: '近1月', exact: true }).click()
  await expect(page.locator('.panel-heading')).toContainText('2026-09-24')
  await page.getByRole('button', { name: '刷新市场情绪数据' }).click()
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
  expect([...new Set(ranges)]).toEqual(['0', '30'])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(errors).toEqual([])
  await expect(page.locator('.section-eyebrow,.eyebrow')).toHaveCount(0)
})

test('sentiment failure retry and empty state preserve module tabs', async ({ page }) => {
  let failed = true
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.route('**/api/v1/market-sentiment/history?*', (route) =>
    failed
      ? route.fulfill({ status: 503, json: { detail: '情绪数据暂不可用' } })
      : route.fulfill({
          json: {
            ...history,
            latest: null,
            points: [],
            summary: { observed_days: 0, cold_days: 0, hot_days: 0 },
          },
        }),
  )
  await page.goto('/market-sentiment')
  await expect(page.getByRole('alert')).toContainText('情绪数据暂不可用')
  await expect(
    page.getByRole('navigation', { name: '市场情绪模块切换' }).getByRole('link'),
  ).toHaveCount(2)
  failed = false
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '暂无市场情绪数据' })).toBeVisible()
  await expect(page.locator('.sentiment-chart')).toHaveCount(0)
})
