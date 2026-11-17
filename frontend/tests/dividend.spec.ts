import { expect, test } from './fixtures'

// Every page now polls collection status independently of the saved result.
// Per-test routes registered later override this default when testing mutations.
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/status', route =>
    route.fulfill({ json: { running: false, available: true } }),
  )
})

function stock(method: string, i: number) {
  return {
    method,
    rank: i + 1,
    code: `${String(i + (method === 'bank' ? 100 : 10)).padStart(6, '0')}.XSHE`,
    trade_date: '2026-10-16',
    name: `示例股票${i + 1}`,
    industry_code: 'test',
    industry_name: method === 'bank' ? '银行' : '公用事业',
    industry_level: 'sw_l3',
    dividend_yield_static: 8 - i * 0.1,
    dividend_yield_history: [2025, 2024, 2023].map((year, n) => ({
      fiscal_year: year,
      yield_pct: 8 - i * 0.1 - n,
    })),
    dividend_cash_annual: null,
    dividend_per_share: (8 - i * 0.1) / 10,
    dividend_fiscal_year: 2025,
    pe_ttm: 6.5,
    pb: 1.1,
    market_cap: 130,
    close: 10,
  }
}
const groups = ['value', 'bank'].map((id) => ({
  id,
  name: id === 'bank' ? '高股息低估值（银行业）' : '高股息低估值（非银行业）',
  ranking_field: 'dividend_yield_static',
  candidates: null,
  items: Array.from({ length: id === 'bank' ? 20 : 50 }, (_, i) => stock(id, i)),
}))
const fixture = {
  snapshot: {
    trade_date: '2026-10-16',
    updated_at: '2026-10-16T17:30:00',
    rule_version: 'dividend_dps_proposal_age3_annual_history_v10',
    universe_count: 5000,
    eligible_count: 4000,
    value_candidates: 80,
    value_count: 70,
  },
  groups,
}

test('static yield history opens on hover or tap without clipping or changing the row', async ({
  page,
}, testInfo) => {
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({ json: fixture }),
  )
  await page.goto('/dividend-selection')
  for (const [group, firstCode] of [
    ['高股息低估值（非银行业）', '000010.XSHE'],
    ['高股息低估值（银行业）', '000100.XSHE'],
  ]) {
    await page.getByRole('link', { name: group, exact: true }).click()
    await expect(page.locator('tbody tr').first()).toContainText(firstCode)
    const marker = page.getByRole('button', { name: '查看近三年股息率' }).first()
    if (testInfo.project.name === 'desktop') await marker.hover()
    else await marker.click()
    const tip = page.getByRole('tooltip')
    await expect(tip).toContainText('2025 财年')
    if (testInfo.project.name === 'mobile') {
      // Touch browsers can synthesize mouseout/blur or finish focus scrolling after click.
      await marker.dispatchEvent('mouseout', { relatedTarget: null })
      await marker.evaluate((button) => (button as HTMLElement).blur())
      await page.evaluate(() => window.dispatchEvent(new Event('scroll')))
      await expect(tip).toBeVisible()
    }
    await expect(tip).toContainText('2023 财年')
    await expect(tip).toContainText('6.00%')
    const box = await tip.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width)
    await page.keyboard.press('Escape')
    await expect(tip).toHaveCount(0)
  }
})

test('dividend module switches two groups, preserves search and omits retired fields on desktop/mobile', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({ json: fixture }),
  )
  await page.goto('/')
  await page.getByRole('link', { name: '进入业绩选股', exact: true }).click()
  await page.getByRole('link', { name: '高股息低估值（非银行业）', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText('高股息低估值')
  await expect(page.locator('tbody tr')).toHaveCount(50)
  await expect(page.locator('tbody tr').first()).toContainText('8.00%')
  await expect(page.locator('.page-header .module-card')).toHaveCount(3)
  await page.getByRole('link', { name: /高股息低估值（银行业）/ }).click()
  await expect(page.locator('tbody tr')).toHaveCount(20)
  await expect(
    page.getByRole('heading', { name: '高股息低估值（银行业） · 前 20 名' }),
  ).toBeVisible()
  await expect(page.locator('tbody tr').first()).toContainText('000100.XSHE')
  await page.getByRole('link', { name: /高股息低估值（非银行业）/ }).click()
  await expect(page.locator('tbody tr')).toHaveCount(50)
  await expect(page.locator('thead th').nth(2)).toHaveText('静态股息率')
  await expect(page.locator('tbody tr').first()).toContainText('000010.XSHE')
  const search = page.getByRole('textbox', { name: '搜索高股息股票' })
  await search.fill('不存在')
  await expect(page.getByRole('heading', { name: '没有匹配的股票' })).toBeVisible()
  await search.fill('000010')
  await expect(page.locator('tbody tr')).toHaveCount(1)
  await search.fill('')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
  await page.evaluate(() => {
    ;(document.activeElement as HTMLElement)?.blur()
    window.scrollTo(0, 0)
  })
  await page.screenshot({
    path: testInfo.outputPath('dividend-folders-viewport.png'),
    animations: 'disabled',
  })
  await page.screenshot({
    path: testInfo.outputPath('dividend.png'),
    fullPage: true,
    animations: 'disabled',
  })
})

test('dividend uncollected, legitimate zero results and retry states', async ({ page }) => {
  let mode = 'empty'
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    mode === 'fail'
      ? route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } })
      : route.fulfill({
          json:
            mode === 'empty'
              ? { ...fixture, snapshot: null }
              : mode === 'zero'
                ? {
                    ...fixture,
                    groups: groups.map((g) => ({ ...g, items: [], candidates: 0 })),
                  }
                : fixture,
        }),
  )
  await page.goto('/dividend-selection')
  await expect(page.getByRole('heading', { name: '暂无高股息选股结果' })).toBeVisible()
  mode = 'zero'
  await page.getByRole('button', { name: '刷新高股息选股' }).click()
  await expect(page.getByRole('heading', { name: '本组暂无符合条件的股票' })).toBeVisible()
  mode = 'fail'
  await page.getByRole('button', { name: '刷新高股息选股' }).click()
  await expect(page.getByRole('alert').filter({ hasText: '数据库暂不可用' })).toBeVisible()
  mode = 'data'
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.locator('tbody tr')).toHaveCount(50)
})

test('legacy successful results remain visible with an old-rule warning', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({
      json: {
        ...fixture,
        requires_refresh: true,
        snapshot: { ...fixture.snapshot, rule_version: 'dividend_value_pe12_age3_v7' },
        groups: groups.map(g => ({ ...g, items: g.items.slice(0, g.id === 'bank' ? 10 : 25) })),
      },
    }),
  )
  await page.goto('/dividend-selection')
  await expect(page.getByRole('status')).toContainText('保留上次成功结果，采用旧版股息率口径')
  await expect(page.getByRole('status')).toContainText('历史两年尚未切换为年度均价')
  await expect(page.locator('tbody tr')).toHaveCount(25)
  await expect(page.getByText('最高股息率（旧口径）')).toBeVisible()
  await page.getByRole('link', { name: /高股息低估值（银行业）/ }).click()
  await expect(page.getByRole('status')).toContainText('保留上次成功结果')
  await expect(page.locator('tbody tr')).toHaveCount(10)
})

test('retired method URL defaults to nonbank and has no obsolete controls', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({ json: fixture }),
  )
  await page.goto('/dividend-selection?method=classic')
  await expect(
    page.getByRole('link', { name: '高股息低估值（非银行业）', exact: true }),
  ).toHaveAttribute('aria-current', 'page')
  await expect(page.locator('tbody tr')).toHaveCount(50)
  await expect(page.getByRole('link', { name: '纯高股息', exact: true })).toHaveCount(0)
  await expect(page.getByRole('checkbox')).toHaveCount(0)
  await expect(page.locator('thead')).not.toContainText('PEG')
  await expect(page.locator('thead')).not.toContainText('累计分红')
})

test('administrator can collect both groups and cancel the shared task', async ({ page }) => {
  let running = false
  let refreshes = 0
  let cancellations = 0
  await page.route('**/api/v1/dividend-selection/latest', route => route.fulfill({ json: fixture }))
  await page.route('**/api/v1/dividend-selection/status', route => route.fulfill({ json: { running, available: true } }))
  await page.route('**/api/v1/dividend-selection/refresh', route => {
    refreshes++; running = true
    return route.fulfill({ status: 202, json: { started: true } })
  })
  await page.route('**/api/v1/dividend-selection/cancel', route => {
    cancellations++; running = false
    return route.fulfill({ status: 202, json: { cancelled: true } })
  })
  await page.goto('/dividend-selection')
  await page.getByRole('button', { name: '更新数据', exact: true }).click()
  await expect(page.getByRole('button', { name: '采集中', exact: true })).toBeDisabled()
  await page.getByRole('link', { name: '高股息低估值（银行业）', exact: true }).click()
  await expect(page.getByRole('button', { name: '采集中', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '取消采集', exact: true }).click()
  await expect(page.getByRole('button', { name: '更新数据', exact: true })).toBeEnabled()
  expect(refreshes).toBe(1)
  expect(cancellations).toBe(1)
})

test('shared earnings task busy is not presented as a successful collection', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', route => route.fulfill({ json: fixture }))
  await page.route('**/api/v1/dividend-selection/status', route => route.fulfill({ json: { running: false, available: true } }))
  await page.route('**/api/v1/dividend-selection/refresh', route => route.fulfill({ status: 202, json: { started: false } }))
  await page.goto('/dividend-selection')
  await page.getByRole('button', { name: '更新数据', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: '其他业绩选股任务正在运行' })).toBeVisible()
})

test('collection rejection is visible and does not hide previous results', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', route => route.fulfill({ json: fixture }))
  await page.route('**/api/v1/dividend-selection/status', route => route.fulfill({ json: { running: false, available: true } }))
  await page.route('**/api/v1/dividend-selection/refresh', route => route.fulfill({ status: 409, json: { detail: '当日数据须在16:00后采集' } }))
  await page.goto('/dividend-selection')
  await page.getByRole('button', { name: '更新数据', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('当日数据须在16:00后采集')
  await expect(page.locator('tbody tr').first()).toContainText('000010.XSHE')
})

test('read-only users do not receive collection controls', async ({ page }) => {
  await page.route('**/api/auth/**', route => route.fulfill({ json: {
    user: { id: 'viewer', username: 'viewer', role: 'viewer', enabled: true, created_at: 0 },
    csrf_token: 'test-csrf', configured: true, https_required: false,
  } }))
  await page.route('**/api/v1/dividend-selection/latest', route => route.fulfill({ json: fixture }))
  await page.goto('/dividend-selection')
  await expect(page.locator('tbody tr').first()).toBeVisible()
  await expect(page.getByRole('button', { name: '更新数据', exact: true })).toHaveCount(0)
})

test('collection action stays outside scrolling tabs on narrow screens and clears on navigation', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', route => route.fulfill({ json: fixture }))
  for (const width of [320, 375]) {
    await page.setViewportSize({ width, height: 812 })
    await page.goto('/dividend-selection')
    const actions = page.locator('.page-header-actions')
    const update = actions.getByRole('button', { name: '更新数据', exact: true })
    await expect(update).toBeEnabled()
    await expect(page.getByRole('button', { name: '更新数据', exact: true })).toHaveCount(1)
    await expect(page.locator('.module-card-switch').getByRole('button', { name: '更新数据' })).toHaveCount(0)
    const tabs = page.locator('.page-header .module-card-switch')
    const tabBox = await tabs.boundingBox()
    const actionBox = await actions.boundingBox()
    expect(actionBox!.x).toBeGreaterThanOrEqual(tabBox!.x + tabBox!.width)
    expect(Math.abs((actionBox!.y + actionBox!.height / 2) - (tabBox!.y + tabBox!.height / 2))).toBeLessThan(2)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await tabs.getByRole('link', { name: '高股息低估值（银行业）', exact: true }).click()
    await expect(update).toBeEnabled()
    await expect(page.getByRole('button', { name: '更新数据', exact: true })).toHaveCount(1)
    await page.getByRole('link', { name: '总览', exact: true }).click()
    await expect(page.locator('.page-header-actions').getByRole('button')).toHaveCount(0)
  }
})
