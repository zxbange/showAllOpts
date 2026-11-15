import { expect, test } from './fixtures'

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
    dividend_cash_annual: 50000,
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
  items: Array.from({ length: id === 'bank' ? 10 : 25 }, (_, i) => stock(id, i)),
}))
const fixture = {
  snapshot: {
    trade_date: '2026-10-16',
    updated_at: '2026-10-16T17:30:00',
    rule_version: 'dividend_value_pe12_v6',
    universe_count: 5000,
    eligible_count: 4000,
    value_candidates: 60,
    value_count: 35,
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
  await expect(page.locator('tbody tr')).toHaveCount(25)
  await expect(page.locator('tbody tr').first()).toContainText('8.00%')
  await expect(page.locator('.page-header .module-card')).toHaveCount(3)
  await page.getByRole('link', { name: /高股息低估值（银行业）/ }).click()
  await expect(page.locator('tbody tr')).toHaveCount(10)
  await expect(
    page.getByRole('heading', { name: '高股息低估值（银行业） · 前 10 名' }),
  ).toBeVisible()
  await expect(page.locator('tbody tr').first()).toContainText('000100.XSHE')
  await page.getByRole('link', { name: /高股息低估值（非银行业）/ }).click()
  await expect(page.locator('tbody tr')).toHaveCount(25)
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
  await expect(page.getByRole('alert')).toContainText('数据库暂不可用')
  mode = 'data'
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.locator('tbody tr')).toHaveCount(25)
})

test('legacy trailing yields are hidden until static results are collected', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({
      json: {
        ...fixture,
        requires_refresh: true,
        groups: groups.map((g) => ({ ...g, items: [] })),
      },
    }),
  )
  await page.goto('/dividend-selection')
  await expect(page.getByText('PE 上限已调整为 12，请运行新版采集脚本并同步。')).toBeVisible()
  await expect(page.locator('tbody tr')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '请更新选股结果' })).toBeVisible()
  await page.getByRole('link', { name: /高股息低估值（非银行业）/ }).click()
  await expect(page.getByRole('heading', { name: '请更新选股结果' })).toBeVisible()
  await expect(page.locator('tbody tr')).toHaveCount(0)
})

test('retired method URL defaults to nonbank and has no obsolete controls', async ({ page }) => {
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({ json: fixture }),
  )
  await page.goto('/dividend-selection?method=classic')
  await expect(
    page.getByRole('link', { name: '高股息低估值（非银行业）', exact: true }),
  ).toHaveAttribute('aria-current', 'page')
  await expect(page.locator('tbody tr')).toHaveCount(25)
  await expect(page.getByRole('link', { name: '纯高股息', exact: true })).toHaveCount(0)
  await expect(page.getByRole('checkbox')).toHaveCount(0)
  await expect(page.locator('thead')).not.toContainText('PEG')
  await expect(page.locator('thead')).not.toContainText('累计分红')
})
