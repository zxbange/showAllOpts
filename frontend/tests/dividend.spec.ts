import { expect, test } from '@playwright/test'

function stock(method: string, i: number) {
  return {
    method,
    rank: i + 1,
    code: `${String(i + (method === 'classic' ? 1 : 10)).padStart(6, '0')}.XSHE`,
    trade_date: '2026-10-16',
    name: `示例股票${i + 1}`,
    industry_code: 'test',
    industry_name: i % 2 ? '银行' : '公用事业',
    industry_level: 'sw_l3',
    dividend_yield_1y: 8 - i * 0.1,
    dividend_ratio_3y: 20 - i * 0.2,
    dividend_cash_1y: 50000,
    dividend_cash_3y: 150000,
    pe_ttm: 6.5,
    pb: 1.1,
    market_cap: 130,
    close: 10,
    revenue_growth: 10,
    profit_growth: 20,
    adjusted_roe: 8,
    peg: 0.325,
    report_period: '2026-06-30',
    publication_date: '2026-08-10',
  }
}
const groups = ['classic', 'value'].map((id) => ({
  id,
  name: id === 'classic' ? '纯高股息' : '高股息低估值',
  ranking_field: id === 'classic' ? 'dividend_ratio_3y' : 'dividend_yield_1y',
  candidates: 60,
  items: Array.from({ length: 25 }, (_, i) => stock(id, i)),
}))
const fixture = {
  snapshot: {
    trade_date: '2026-10-16',
    updated_at: '2026-10-16T17:30:00',
    rule_version: 'dividend_top25_v1',
    universe_count: 5000,
    eligible_count: 4000,
    classic_candidates: 60,
    value_candidates: 60,
    classic_count: 25,
    value_count: 25,
  },
  groups,
  overlap_codes: groups[0].items
    .filter((s) => groups[1].items.some((t) => t.code === s.code))
    .map((s) => s.code),
}

test('dividend module switches rules, searches and shows overlap on desktop/mobile', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({ json: fixture }),
  )
  await page.goto('/')
  await page.getByRole('link', { name: '进入高股息选股', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText('高股息选股')
  await expect(page.locator('tbody tr')).toHaveCount(25)
  await expect(page.locator('tbody tr').first()).toContainText('20.00%')
  await page.getByRole('button', { name: /高股息低估值/ }).click()
  await expect(page.locator('thead th').nth(2)).toHaveText('近一年股息率')
  await expect(page.locator('tbody tr').first()).toContainText('000010.XSHE')
  await page.getByRole('checkbox', { name: '只看两组共同入选' }).check()
  await expect(page.locator('tbody tr')).toHaveCount(16)
  const search = page.getByRole('textbox', { name: '搜索高股息股票' })
  await search.fill('不存在')
  await expect(page.getByRole('heading', { name: '没有匹配的股票' })).toBeVisible()
  await search.fill('000010')
  await expect(page.locator('tbody tr')).toHaveCount(1)
  await search.fill('')
  await page.getByRole('checkbox', { name: '只看两组共同入选' }).uncheck()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
  await page.evaluate(() => {
    ;(document.activeElement as HTMLElement)?.blur()
    window.scrollTo(0, 0)
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
                    overlap_codes: [],
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
