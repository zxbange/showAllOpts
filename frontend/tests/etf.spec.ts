import { expect, test } from './fixtures'
import { etfGroups } from '../src/lib/etf'

const names = [
  '沪深300ETF华泰柏瑞',
  '沪深300ETF易方达',
  '沪深300ETF华夏',
  '沪深300ETF嘉实',
  '沪深300ETF天弘',
]
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/etf-shares/collection-status', (route) =>
    route.fulfill({ json: { status: 'missing_writer_configuration' } }),
  )
})

async function expectCollectionLast(page: import('./fixtures').Page, content: string) {
  const panel = page.getByRole('region', { name: 'ETF采集管理' })
  await expect(panel).toBeVisible()
  await expect(page.locator('.etf-page > :last-child')).toHaveAttribute('aria-label', 'ETF采集管理')
  const contentBox = await page.locator(content).boundingBox()
  const panelBox = await panel.boundingBox()
  expect(contentBox).not.toBeNull()
  expect(panelBox!.y).toBeGreaterThanOrEqual(contentBox!.y + contentBox!.height)
}

const codes = ['510300.XSHG', '510310.XSHG', '510330.XSHG', '159919.XSHE', '515330.XSHG']
function fixture(group = 'hs300') {
  const dates = Array.from({ length: 70 }, (_, i) =>
    new Date(Date.UTC(2026, 5, 1 + i)).toISOString().slice(0, 10),
  ).filter((day) => ![0, 6].includes(new Date(day).getUTCDay()))
  const funds = names.map((name, i) => ({
    code: codes[i],
    name: name.replace('沪深300', etfGroups.find((item) => item.id === group)!.name),
    date: dates[dates.length - (i === 4 ? 2 : 1)],
    shares: String([28553000000, 16879000000, 652000000, 8689000000, 686000000][i]),
    previous_date: dates[dates.length - (i === 4 ? 3 : 2)],
    shares_change: i === 2 ? null : String(i === 3 ? -65000000 : 3068000000 / (i + 1)),
    daily_change_pct: i === 2 ? null : i === 3 ? -0.75 : 12.04 / (i + 1),
    updated_at: '2026-08-09T18:00:00',
    lagging: i === 4,
    points: dates.slice(0, dates.length - (i === 4 ? 1 : 0)).map((date, j) => ({
      date,
      shares: String(
        Math.round([280, 160, 6, 85, 7][i] * 1e8 * (1 + 0.04 * Math.sin(j / 5 + i) + j / 600)),
      ),
      change_pct: j / 6 + Math.sin(j / 5) * 3,
    })),
  }))
  return {
    group,
    group_name: etfGroups.find((item) => item.id === group)!.name + ' ETF',
    start_date: dates[0],
    end_date: dates[dates.length - 1],
    baseline_date: dates[0],
    observed_days: dates.length,
    available_funds: 5,
    funds,
    market_values: dates.map((date, i) => ({
      date,
      market_value: i === 3 ? null : String(500e8 + i * 1e8),
      available_funds: i === 3 ? 4 : 5,
      expected_funds: 5,
      quality_flags: i === 0 ? ['suspected_split'] : [],
    })),
  }
}

test('one ETF entry, seven indices, aggregate toggle, range and history navigation', async ({
  page,
}, testInfo) => {
  const requests: URL[] = []
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/etf-shares/history?*', (route) => {
    const url = new URL(route.request().url())
    requests.push(url)
    return route.fulfill({ json: fixture(url.searchParams.get('group') ?? 'hs300') })
  })
  await page.goto('/market-crowding')
  await page.getByRole('link', { name: '国家队动态', exact: true }).click()
  await expect(page.getByRole('combobox', { name: '跟踪指数' })).toHaveValue('hs300')
  await expect(page.locator('.etf-card')).toHaveCount(5)
  await expect(page.locator('.etf-card').first()).toContainText('285.53')
  await expect(page.locator('.etf-card').nth(2)).toContainText('日变动 —')
  await expect(page.locator('.etf-card').last()).toContainText('待更新')
  await expect(page.getByRole('img', { name: 'ETF总份额曲线' }).locator('canvas')).toBeVisible()
  await expectCollectionLast(page, '.etf-trend-panel')
  await expect(page.getByText('切换指数，查看 ETF 总份额与所跟踪基金的合计流通市值。')).toHaveCount(
    0,
  )
  await expect(page.getByText('点击卡片聚焦单只 ETF，再次点击恢复全部')).toHaveCount(0)
  await expect(page.locator('.etf-method-note')).toHaveCount(0)
  await expect(page.getByText(/日变动对比前一交易日|清单不覆盖该指数全部/)).toHaveCount(0)
  await page.getByRole('button', { name: '查看沪深300ETF华泰柏瑞曲线' }).click()
  await expect(
    page.getByRole('heading', { name: '总份额变化曲线 · 沪深300ETF华泰柏瑞' }),
  ).toBeVisible()
  await page.getByRole('button', { name: '查看沪深300ETF华泰柏瑞曲线' }).click()
  await expect(
    page.getByRole('heading', { name: '总份额变化曲线 · 全部', exact: true }),
  ).toBeVisible()
  await page.getByRole('button', { name: '查看沪深300ETF华泰柏瑞曲线' }).click()
  await page.getByRole('button', { name: '显示全部 ETF' }).click()
  await expect(
    page.getByRole('heading', { name: '总份额变化曲线 · 全部', exact: true }),
  ).toBeVisible()
  await page.getByRole('button', { name: '流通市值', exact: true }).click()
  await expect(
    page.getByRole('img', { name: 'ETF合计流通市值曲线' }).locator('canvas'),
  ).toBeVisible()
  await expectCollectionLast(page, '.etf-method-note')
  await expect(page.getByText('流通市值口径', { exact: true })).toBeVisible()
  await expect(page.getByText(/1 个交易日因疑似份额拆并或异常跳变/)).toBeVisible()
  await expect(page.getByText(/深市仅使用已核验的收盘价日期/)).toBeVisible()
  await expect(page.getByText(/清单不覆盖该指数全部/)).toHaveCount(0)
  await expect(page.locator('.etf-card')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '显示全部 ETF' })).toHaveCount(0)
  await expect(page.getByText('同日份额 × 收盘价后求和；缺数据不估算')).toBeVisible()
  await page.getByRole('button', { name: '近1月', exact: true }).click()
  await expect.poll(() => requests.at(-1)?.searchParams.get('days')).toBe('30')
  for (const group of etfGroups.slice(1)) {
    await page.getByRole('combobox', { name: '跟踪指数' }).selectOption(group.id)
    await expect(page.getByRole('heading', { name: `合计流通市值 · ${group.name}` })).toBeVisible()
    await expect(page.getByRole('img', { name: 'ETF合计流通市值曲线' })).toBeVisible()
    expect(requests.at(-1)?.searchParams.get('group')).toBe(group.id)
    expect(requests.at(-1)?.searchParams.get('days')).toBe('30')
  }
  await page.getByRole('button', { name: '总份额', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '总份额变化曲线 · 全部', exact: true }),
  ).toBeVisible()
  await page.reload()
  await expect(page.locator('.etf-card').first()).toContainText('上证180')
  await expect(page.getByRole('link', { name: '国家队动态', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.goBack()
  await expect(page.locator('.etf-card').first()).toContainText('上证50')
  await page.goForward()
  await expect(page.locator('.etf-card').first()).toContainText('上证180')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('etf-dashboard.png'),
    fullPage: true,
    animations: 'disabled',
  })
  expect(errors).toEqual([])
})

test('failure retry, empty database, legacy market data and empty selected fund', async ({
  page,
}) => {
  let state = 'error'
  await page.route('**/api/v1/etf-shares/history?*', (route) => {
    if (state === 'error') return route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } })
    const data = fixture(new URL(route.request().url()).searchParams.get('group') ?? 'hs300')
    if (state === 'empty')
      return route.fulfill({
        json: { ...data, end_date: null, observed_days: 0, funds: [], market_values: [] },
      })
    return route.fulfill({
      json: {
        ...data,
        market_values: undefined,
        baseline_date: null,
        available_funds: 4,
        funds: data.funds.map((fund, i) =>
          i === 0
            ? {
                ...fund,
                shares: null,
                date: null,
                points: [],
                shares_change: null,
                daily_change_pct: null,
              }
            : fund,
        ),
      },
    })
  })
  await page.goto('/etf-shares?group=invalid')
  await expect(page.locator('.state-panel[role="alert"]')).toContainText('数据库暂不可用')
  await expectCollectionLast(page, '.state-panel')
  await expect(page.getByRole('combobox', { name: '跟踪指数' })).toHaveValue('hs300')
  state = 'empty'
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '暂无ETF份额数据' })).toBeVisible()
  await expectCollectionLast(page, '.state-panel')
  await page.getByRole('button', { name: '流通市值', exact: true }).click()
  await expect(page.getByRole('heading', { name: '流通市值数据未齐' })).toBeVisible()
  await expect(page.getByRole('img', { name: 'ETF合计流通市值曲线' })).toHaveCount(0)
  state = 'partial'
  await page.getByRole('button', { name: '刷新ETF数据' }).click()
  await expect(page.getByRole('heading', { name: '流通市值数据未齐' })).toBeVisible()
  await page.getByRole('button', { name: '总份额', exact: true }).click()
  await page.getByRole('button', { name: '查看沪深300ETF华泰柏瑞曲线' }).click()
  await expect(page.getByRole('heading', { name: '该ETF在所选区间暂无数据' })).toBeVisible()
  state = 'error'
  await page.getByRole('combobox', { name: '跟踪指数' }).selectOption('zz500')
  await expect(page.locator('.state-panel[role="alert"]')).toContainText('数据库暂不可用')
  await expectCollectionLast(page, '.state-panel')
  await expect(page.locator('.etf-card')).toHaveCount(0)
  state = 'partial'
  await page.getByRole('combobox', { name: '跟踪指数' }).selectOption('cyb')
  await expect(
    page.getByRole('heading', { name: '总份额变化曲线 · 全部', exact: true }),
  ).toBeVisible()
})

test('rapid index, range and metric changes never show a stale response', async ({ page }) => {
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  let delayedStarted = false
  await page.route('**/api/v1/etf-shares/history?*', async (route) => {
    const group = new URL(route.request().url()).searchParams.get('group') ?? 'hs300'
    if (group === 'zz500') {
      delayedStarted = true
      await pending
    }
    await route.fulfill({ json: fixture(group) }).catch(() => {})
  })
  await page.goto('/etf-shares')
  await expect(page.locator('.etf-card')).toHaveCount(5)
  await page.getByRole('combobox', { name: '跟踪指数' }).selectOption('zz500')
  await expect.poll(() => delayedStarted).toBe(true)
  await expect(page.getByRole('status', { name: '正在加载数据' })).toBeVisible()
  await expectCollectionLast(page, '.loading-state')
  await expect(page.locator('.etf-card')).toHaveCount(0)
  await page.getByRole('button', { name: '流通市值', exact: true }).click()
  await page.getByRole('button', { name: '近1月', exact: true }).click()
  await page.getByRole('combobox', { name: '跟踪指数' }).selectOption('kc50')
  await expect(page.getByRole('heading', { name: '合计流通市值 · 科创50' })).toBeVisible()
  release()
  await page.getByRole('button', { name: '总份额', exact: true }).click()
  await expect(page.locator('.etf-card').first()).toContainText('科创50')
  await expect(page.getByRole('combobox', { name: '跟踪指数' })).toHaveValue('kc50')
  await expect(page.getByRole('button', { name: '近1月', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
})

test('missing writer is clearly visible without blocking saved charts', async ({ page }) => {
  await page.route('**/api/v1/etf-shares/history?*', (route) => route.fulfill({ json: fixture() }))
  await page.goto('/etf-shares')
  await expect(
    page.getByText('自动采集未就绪：尚未配置数据库连接。页面同步只读取已有数据。'),
  ).toBeVisible()
  await expect(page.getByRole('img', { name: 'ETF总份额曲线' })).toBeVisible()
})
