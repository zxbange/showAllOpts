import { expect, test } from '@playwright/test'

const modules = [
  '行业动量',
  '抱团拥挤度',
  '微盘温度计',
  'ETF份额仪表盘',
  '净利润断层',
  '微盘新高新低',
  '高股息选股',
  '高增长',
]
const fixture = {
  enabled: true,
  refreshing: false,
  manual_enabled: true,
  auto_refresh: true,
  daily_time: '20:00',
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
      'high-growth',
    ][i],
    name,
    ready: true,
    updated_at: '2026-10-18T18:00:00+08:00',
    last_attempt_at: '2026-10-18T18:00:00+08:00',
    rows: 100,
    error: null,
  })),
}

test('cache panel shows old snapshot and allows module synchronization without credentials', async ({
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
    expect(route.request().headers()['authorization']).toBeUndefined()
    posted = route.request().postDataJSON()
    running = true
    await route.fulfill({ status: 202, json: { accepted: true } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  const panel = page.getByRole('dialog')
  await expect(panel).toBeVisible()
  await expect(panel).toContainText('每天北京时间 20:00 自动同步')
  await expect(panel.getByText('保留旧快照', { exact: true })).toBeVisible()
  await expect(panel.locator('.cache-row')).toHaveCount(8)
  await page.getByLabel('同步范围', { exact: true }).selectOption('dividend-selection')
  await expect(panel.locator('input')).toHaveCount(0)
  await page.getByRole('button', { name: '立即同步', exact: true }).click()
  await expect(page.getByRole('button', { name: '同步进行中…', exact: true })).toBeDisabled()
  expect(posted).toEqual({ module: 'dividend-selection' })
  await page.screenshot({ path: testInfo.outputPath('cache-panel.png'), fullPage: true })
  expect(await panel.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
  await page.getByRole('button', { name: '关闭数据同步' }).click()
  await expect(panel).not.toBeVisible()
})

test('all modules and each individual module can be synchronized directly', async ({ page }) => {
  const posted: unknown[] = []
  await page.route('**/api/v1/cache/status', (route) => route.fulfill({ json: fixture }))
  await page.route('**/api/v1/cache/refresh', (route) => {
    expect(route.request().headers()['authorization']).toBeUndefined()
    posted.push(route.request().postDataJSON())
    return route.fulfill({ status: 202, json: { accepted: true } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  const select = page.getByLabel('同步范围', { exact: true })
  const button = page.getByRole('button', { name: '立即同步', exact: true })
  await expect(button).toBeEnabled()
  const choices = ['', ...fixture.modules.map((item) => item.id)]
  for (const [index, module] of choices.entries()) {
    await select.selectOption(module)
    await button.click()
    await expect.poll(() => posted.length).toBe(index + 1)
    await expect(button).toBeEnabled()
    expect(posted[index]).toEqual({ module: module || null })
  }
})

test('direct database mode has no cache sync action', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({ json: { ...fixture, enabled: false, manual_enabled: false, modules: [] } }),
  )
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('当前使用数据库直连')
  await expect(page.getByRole('button', { name: '立即同步', exact: true })).toHaveCount(0)
})

test('refresh failure is shown and the action becomes available again', async ({ page }) => {
  await page.route('**/api/v1/cache/status', (route) => route.fulfill({ json: fixture }))
  await page.route('**/api/v1/cache/refresh', (route) =>
    route.fulfill({ status: 409, json: { detail: '已有同步任务正在执行，请等待完成。' } }),
  )
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  const button = page.getByRole('button', { name: '立即同步', exact: true })
  await button.click()
  await expect(page.getByRole('dialog')).toContainText('已有同步任务正在执行，请等待完成。')
  await expect(button).toBeEnabled()
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
  await expect(page.getByRole('dialog').locator('.cache-row')).toHaveCount(8)
  const initial = pageReads
  updated = '2026-10-18T18:10:00+08:00'
  await expect.poll(() => pageReads, { timeout: 10000 }).toBeGreaterThan(initial)
})

test('per-module progress shows running rows and completed bar', async ({ page }) => {
  const data = structuredClone(fixture)
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({
      json: {
        ...data,
        refreshing: true,
        modules: data.modules.map((m, i) => ({
          ...m,
          progress: {
            status: i === 0 ? 'running' : 'complete',
            stage: i === 0 ? '读取持仓表' : '同步完成',
            percent: i === 0 ? 35 : 100,
            processed: 2000,
            elapsed_seconds: 12,
          },
        })),
      },
    }),
  )
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  const bars = page.getByRole('progressbar')
  await expect(bars).toHaveCount(data.modules.length)
  await expect(bars.first()).toHaveAttribute('value', '35')
  await expect(bars.nth(1)).toHaveAttribute('value', '100')
  await expect(page.getByText(/读取持仓表/)).toContainText('2,000')
  await expect(page.getByRole('button', { name: '同步进行中…', exact: true })).toBeDisabled()
})


test('retry clears failure and manual stop waits for database boundary', async ({ page }) => {
  let running = false
  let cancelling = false
  const runId = 'a'.repeat(32)
  await page.route('**/api/v1/cache/status', route => route.fulfill({ json: {
    ...fixture, refreshing: running, run_id: running ? runId : null, cancelling,
    modules: fixture.modules.map(m => ({ ...m,
      error: running ? null : '同步失败，保留上次成功数据。',
      progress: { status: running ? 'running' : 'failed', stage: '读取数据', percent: 20 },
    })),
  } }))
  await page.route('**/api/v1/cache/refresh', async route => {
    running = true
    await route.fulfill({ status: 202, json: { accepted: true } })
  })
  await page.route('**/api/v1/cache/cancel', async route => {
    expect(route.request().postDataJSON()).toEqual({ run_id: runId })
    cancelling = true
    await route.fulfill({ status: 202, json: { accepted: true } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  await expect(page.locator('.cache-row-error')).toHaveCount(8)
  await page.getByRole('button', { name: '立即同步', exact: true }).click()
  await expect(page.locator('.cache-row-error')).toHaveCount(0)
  await page.getByRole('button', { name: '停止本次同步', exact: true }).click()
  await expect(page.getByRole('button', { name: '正在停止…', exact: true })).toBeDisabled()
  await expect(page.getByText('正在等待当前数据库操作结束；保留成功快照和续传断点。')).toBeVisible()
})
