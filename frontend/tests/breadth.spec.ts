import { expect, test } from '@playwright/test'

const points = Array.from({ length: 50 }, (_, i) => ({
  date: new Date(Date.UTC(2026, 7, 1 + i)).toISOString().slice(0, 10),
  sample_count: 400,
  strength: i / 2 - 10,
  sentiment: i - 20,
  change_6d: i >= 6 ? 3 : null,
  change_12d: i >= 12 ? 6 : null,
  market_state: '偏强',
  periods: [5, 10, 20, 30].map((period, j) => ({
    period,
    high_count: 150 + i - j * 10,
    low_count: 130 - i - j * 10,
    high_ratio: (150 + i - j * 10) / 4,
    low_ratio: (130 - i - j * 10) / 4,
    spread_pp: (20 + i * 2) / 4,
    dominant_days: 10,
    observed_days: 15,
    dominance_pct: (100 * 10) / 15,
  })),
}))

function fixture(days = 180) {
  const selected = points.slice(-days)
  return {
    series_id: 'micro400_hl_v1',
    start_date: selected[0].date,
    end_date: selected.at(-1)!.date,
    latest: { ...points.at(-1)!, updated_at: '2026-09-19T17:00:00' },
    observed_days: selected.length,
    points: selected,
  }
}

test('overview to breadth, cycles, chart modes and range on desktop/mobile', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  const ranges: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.route('**/api/v1/microcap-breadth/history?*', (route) => {
    const days = new URL(route.request().url()).searchParams.get('days')!
    ranges.push(days)
    return route.fulfill({ json: fixture(Number(days)) })
  })
  await page.goto('/')
  await page.getByRole('link', { name: '进入微盘新高新低', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText('微盘新高新低')
  await expect(page.locator('.breadth-period')).toHaveCount(4)
  await expect(page.locator('.breadth-chart canvas')).toHaveCount(2)
  await page.getByRole('button', { name: /30 日观察/ }).click()
  await expect(page.getByRole('heading', { name: '30 日新高新低趋势', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '样本占比', exact: true }).click()
  await expect(page.getByRole('button', { name: '样本占比', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await page.getByRole('button', { name: '新高－新低差', exact: true }).click()
  await page.getByRole('button', { name: '情绪加权值', exact: true }).click()
  await page.getByRole('button', { name: '近 30 日', exact: true }).click()
  await expect.poll(() => ranges.includes('30')).toBe(true)
  await expect(page.getByText('30 个已采集交易日 · 拖动滑块缩放')).toBeVisible()
  await expect(page.locator('.breadth-table tbody tr')).toHaveCount(10)
  await expect(page.getByText('表示行情宽度的持续性，不是交易胜率。')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
  await page.screenshot({
    path: testInfo.outputPath('breadth.png'),
    fullPage: true,
    animations: 'disabled',
  })
})

test('breadth empty state and failure recovery', async ({ page }) => {
  let status: 'empty' | 'error' | 'ready' = 'empty'
  await page.route('**/api/v1/microcap-breadth/history?*', (route) =>
    status === 'error'
      ? route.fulfill({ status: 503, json: { detail: '暂时无法读取数据' } })
      : route.fulfill({
          json:
            status === 'empty'
              ? { ...fixture(), latest: null, points: [], observed_days: 0 }
              : fixture(),
        }),
  )
  await page.goto('/microcap-breadth')
  await expect(page.getByRole('heading', { name: '暂无微盘新高新低数据' })).toBeVisible()
  status = 'error'
  await page.getByRole('button', { name: '刷新微盘新高新低', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('暂时无法读取数据')
  status = 'ready'
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.locator('.breadth-period')).toHaveCount(4)
})
