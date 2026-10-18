import { expect, test } from '@playwright/test'

const modules = [
  '行业动量',
  '抱团拥挤度',
  '微盘温度计',
  'ETF份额仪表盘',
  '净利润断层',
  '微盘新高新低',
  '高股息选股',
]
const fixture = {
  enabled: true,
  refreshing: false,
  manual_enabled: true,
  auto_refresh: true,
  daily_time: '18:00',
  timezone: 'Asia/Shanghai',
  modules: modules.map((name, i) => ({
    id: [
      'industry-momentum',
      'market-crowding',
      'microcap-temperature',
      'etf-shares',
      'earnings-gap',
      'microcap-breadth',
      'dividend-selection',
    ][i],
    name,
    ready: true,
    updated_at: '2026-10-18T18:00:00+08:00',
    last_attempt_at: '2026-10-18T18:00:00+08:00',
    rows: 100,
    error: null,
  })),
}

test('cache panel shows old snapshot and allows authorized module synchronization', async ({
  page,
}, testInfo) => {
  let running = false
  let posted: unknown = null
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({
      json: {
        ...fixture,
        refreshing: running,
        modules: fixture.modules.map((m, i) =>
          i === 0 ? { ...m, error: '同步失败，保留上次成功数据。' } : m,
        ),
      },
    }),
  )
  await page.route('**/api/v1/cache/refresh', async (route) => {
    expect(route.request().headers()['authorization']).toBe('Bearer test-sync-key')
    posted = route.request().postDataJSON()
    running = true
    await route.fulfill({ status: 202, json: { accepted: true } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  const panel = page.getByRole('dialog')
  await expect(panel).toBeVisible()
  await expect(panel).toContainText('每天北京时间 18:00 自动同步')
  await expect(panel.getByText('保留旧快照', { exact: true })).toBeVisible()
  await expect(panel.locator('.cache-row')).toHaveCount(7)
  await page.getByLabel('同步范围', { exact: true }).selectOption('dividend-selection')
  await page.getByLabel('同步密钥', { exact: true }).fill('test-sync-key')
  await page.getByRole('button', { name: '立即同步', exact: true }).click()
  await expect(page.getByRole('button', { name: '同步进行中…', exact: true })).toBeDisabled()
  expect(posted).toEqual({ module: 'dividend-selection' })
  await expect(page.getByLabel('同步密钥', { exact: true })).toHaveValue('')
  expect(
    await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)),
  ).not.toContain('test-sync-key')
  await page.screenshot({ path: testInfo.outputPath('cache-panel.png'), fullPage: true })
  expect(await panel.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
  await page.getByRole('button', { name: '关闭数据同步' }).click()
  await expect(panel).not.toBeVisible()
})

test('web sync can be disabled without hiding existing cached data', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({ json: { ...fixture, manual_enabled: false } }),
  )
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('网页手动同步尚未启用')
  await expect(page.getByRole('dialog').locator('.cache-row')).toHaveCount(7)
  await expect(page.getByRole('button', { name: '立即同步', exact: true })).toHaveCount(0)
})

test('completed snapshots reload an already open module', async ({ page }) => {
  let updated = '2026-10-18T18:00:00+08:00'
  let pageReads = 0
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({
      json: { ...fixture, modules: fixture.modules.map((m) => ({ ...m, updated_at: updated })) },
    }),
  )
  await page.route('**/api/v1/industry-momentum/latest', (route) => {
    pageReads += 1
    return route.fulfill({
      json: {
        industry_type: 'sw_l2',
        trade_date: null,
        previous_trade_date: null,
        updated_at: null,
        industries: [],
        summary: { industry_count: 0, active_count: 0, average_momentum: 0, max_momentum: 0 },
      },
    })
  })
  await page.goto('/industry-momentum')
  await expect.poll(() => pageReads).toBeGreaterThan(0)
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  await expect(page.getByRole('dialog').locator('.cache-row')).toHaveCount(7)
  const initial = pageReads
  updated = '2026-10-18T18:10:00+08:00'
  await expect.poll(() => pageReads, { timeout: 10000 }).toBeGreaterThan(initial)
})
