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
        status: { running: false, automatic: true },
      },
    })
  })
  await page.goto('/commodities')
  await expect(page.getByRole('heading', { name: 'A00铝', exact: true })).toBeVisible()
  await expect(
    page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '大宗商品' }),
  ).toHaveAttribute('aria-current', 'page')
  await expect(page.locator('.commodity-chart canvas')).toBeVisible()
  await expect(page.getByText('每天 11:00、18:00 自动采集 · 北京时间。')).toBeVisible()
  await expect(page.getByRole('cell', { name: '未核验', exact: true })).toHaveCount(2)
  await expect(page.getByText('现货报价，不是开高低收K线', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '更新 / 补采' }).click()
  expect(updates).toBe(1)
  await expect(page.getByRole('button', { name: '更新 / 补采' })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('commodities.png'), fullPage: true })
})
