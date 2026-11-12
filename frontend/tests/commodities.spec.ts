import { readFileSync } from 'node:fs'
import { test, expect } from './fixtures'
test('A00 ranges and missing evidence, navigation and responsive table', async ({ page }, info) => {
  let updates = 0
  await page.route('**/api/v1/commodities/**', async (route) => {
    if (route.request().method() === 'POST') {
      updates++
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      return route.fulfill({ json: { started: true } })
    }
    return route.fulfill({
      json: {
        rows: JSON.parse(readFileSync('../backend/app/data/a00_changjiang.json', 'utf8')).rows.map(
          (q: any) => (q.rangeEvidence === 'page' ? q : { ...q, low: null, high: null }),
        ),
        status: {
          running: false,
          automatic: true,
          stage: '目标日报价读取失败',
          checkedAt: '2026-11-03T03:00:00Z',
          latestQuoteDate: '2026-11-02',
          targetDate: '2026-11-03',
          targetMode: 'current_spot',
          targetResult: 'sourceReadFailed',
          pagesFetched: 3,
          pagesParsed: 1,
          inserted: 0,
          changed: 0,
          recovery: { day: '2026-11-03', result: 'unavailable' },
          pendingCount: 5,
          pendingTargets: [
            '2026-11-03',
            '2026-11-04',
            '2026-11-05',
            '2026-11-07',
            '2026-11-08',
          ].map((day) => ({ day })),
        },
      },
    })
  })
  await page.goto('/commodities')
  await expect(page.getByRole('heading', { name: 'A00铝', exact: true })).toBeVisible()
  await expect(page.getByText('每日报价区间与均价 · 元/吨 · 自2026-10-08')).toHaveCount(0)
  await expect(page.getByText(/失败日期回补：/)).toHaveCount(0)
  await expect(page.getByText(/待核验日期.*个：/)).toHaveCount(0)
  await expect(page.getByText(/每次最多尝试一个失败日/)).toHaveCount(0)
  await expect(
    page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '大宗商品' }),
  ).toHaveAttribute('aria-current', 'page')
  await expect(page.locator('.commodity-chart canvas')).toBeVisible()
  await expect(page.getByText('每天 11:00、18:00 自动采集 · 北京时间')).toHaveCount(0)
  await expect(page.getByRole('cell', { name: '未核验', exact: true })).toHaveCount(2)
  await expect(page.getByText('现货报价，不是开高低收K线', { exact: true })).toBeVisible()
  await expect(page.getByText('目标日报价读取失败', { exact: true })).toBeVisible()
  await expect(page.locator('.commodity-collection-status')).toContainText('2026-11-03')
  await expect(page.locator('.commodity-collection-status')).toContainText('2026-11-02')
  await page.getByRole('button', { name: '更新当日报价' }).click()
  expect(updates).toBe(1)
  await expect(page.getByRole('button', { name: '更新当日报价' })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('commodities.png'), fullPage: true })
})
