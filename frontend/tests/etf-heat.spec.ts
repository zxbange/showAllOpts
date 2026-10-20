import { expect, test } from '@playwright/test'
const funds = [
  ['518880.XSHG', '黄金ETF', '商品ETF', 1.2345, true],
  ['513100.XSHG', '纳指ETF', '跨境ETF', 0.41, false],
  ['510300.XSHG', '沪深300ETF', '宽基ETF', -0.16, false],
  ['511220.XSHG', '城投债ETF', '债券ETF', null, null],
] as const
const items = funds.map(([code, name, category, score, pass], i) => ({
  date: '2026-10-20',
  code,
  name,
  score,
  close: 3.21,
  annual_return: 0.42,
  r2: 0.89,
  reference_pass: pass,
  rank: score === null ? null : i + 1,
  rank_change: i === 0 ? 2 : 0,
  details: {
    status: score === null ? 'missing' : 'ok',
    reason: '有效价格不足26期或当日停牌',
    checks:
      score === null
        ? undefined
        : { score_range: true, drawdown: true, intraday: true, volume_daily: true, rsrs: pass },
  },
  fund: {
    code,
    name,
    category,
    target_name: name,
    size_yi: '120.52',
    size_date: '2026-10-19',
    size_source: '同日基金份额 × 单位净值',
    fund_type: 'ETF',
  },
}))

test('ETF heat rankings, filter, drilldown and responsive curves', async ({ page }, info) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/etf-momentum/latest', (r) =>
    r.fulfill({
      json: {
        trade_date: '2026-10-20',
        updated_at: '2026-10-20T17:00:00',
        previous_date: '2026-10-19',
        items,
      },
    }),
  )
  await page.route('**/api/v1/etf-momentum/funds/*/history*', (r) =>
    r.fulfill({
      json: {
        code: items[0].code,
        name: items[0].name,
        end_date: '2026-10-20',
        fund: items[0].fund,
        points: [
          { ...items[0], date: '2026-10-16', score: -0.3 },
          { ...items[0], date: '2026-10-19', score: null },
          { ...items[0], score: 1.2345 },
        ],
      },
    }),
  )
  await page.goto('/etf-momentum')
  await expect(page.locator('.etf-heat-tile')).toHaveCount(4)
  await expect(page.locator('.etf-heat-tile').nth(2)).toContainText('-0.1600')
  await expect(page.locator('.etf-heat-tile').nth(3)).toContainText('—')
  await expect(page.getByText('收盘参考条件通过', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '商品ETF', exact: true }).click()
  await expect(page.locator('.etf-heat-tile')).toHaveCount(1)
  await page.getByRole('button', { name: '全部', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索ETF' }).fill('未匹配')
  await expect(page.getByRole('heading', { name: '没有匹配的基金' })).toBeVisible()
  await page.getByRole('textbox', { name: '搜索ETF' }).fill('')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({
    path: info.outputPath('etf-heat.png'),
    fullPage: true,
    animations: 'disabled',
  })
  await page.locator('.etf-heat-tile').first().click()
  await expect(page.getByRole('heading', { name: '黄金ETF', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '动量热度曲线' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '收盘价（不复权）' })).toBeVisible()
  await expect(page.locator('canvas')).toHaveCount(2)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({
    path: info.outputPath('etf-heat-detail.png'),
    fullPage: true,
    animations: 'disabled',
  })
})

test('ETF heat retry and empty states', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  let failed = true
  await page.route('**/api/v1/etf-momentum/latest', (r) =>
    r.fulfill(
      failed
        ? { status: 503, json: { detail: '同步尚未完成' } }
        : { json: { trade_date: null, items: [] } },
    ),
  )
  await page.goto('/etf-momentum')
  await expect(page.getByRole('alert')).toContainText('同步尚未完成')
  failed = false
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: 'ETF热度尚未到达' })).toBeVisible()
})
