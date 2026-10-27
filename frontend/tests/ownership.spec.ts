import { expect, test, type Route } from '@playwright/test'
const stock = { code: '600001.XSHG', name: '示例科技', industry: '半导体' }
const fixture = {
  stock,
  reports: [
    {
      date: '2026-06-30',
      payload: {
        categories: {
          public_fund: { market_value: 1e9, holder_count: 30 },
          // Old cached reports still contain estimated values; the chart must use shares.
          social: { shares: 2000000, market_value: 2e8, holder_count: 2 },
          qfii: { shares: 1000000, market_value: 9e8, holder_count: 1 },
          broker: { shares: 3000000, market_value: 9e8, holder_count: 1 },
          insurance: { shares: 4000000, market_value: 9e8, holder_count: 1 },
          trust: { shares: 5000000, market_value: 9e8, holder_count: 1 },
          private: { shares: null, market_value: 9e8, holder_count: 1 },
        },
      },
    },
    {
      date: '2026-09-30',
      payload: {
        categories: {
          public_fund: { market_value: 1.2e9, holder_count: 35 },
          // New reports have no estimated value fields.
          social: { shares: 3500000, holder_count: 3 },
          qfii: { shares: 1500000, holder_count: 1 },
          broker: { shares: 3200000, holder_count: 1 },
          insurance: { shares: 4500000, holder_count: 1 },
          trust: { shares: 5100000, holder_count: 1 },
          private: { shares: 6000000, holder_count: 1 },
        },
      },
    },
  ],
  holders: [
    { date: '2026-06-30', pub_date: '2026-08-20', payload: { a_share_holders: 30000 } },
    { date: '2026-09-30', pub_date: '2026-10-20', payload: { a_share_holders: 27000 } },
  ],
  holder_change: -3000,
  holder_change_pct: -10,
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/institution-ownership/stocks?*', (r) =>
    r.fulfill({ json: { stocks: [stock] } }),
  )
  await page.route('**/api/v1/institution-ownership/stocks/600001.XSHG', (r) =>
    r.fulfill({ json: fixture }),
  )
  await page.route('**/api/v1/institution-ownership/social-entries?*', (r) =>
    r.fulfill({
      json: {
        period: '2026-09-30',
        periods: ['2026-09-30', '2026-06-30'],
        comparable_count: 100,
        unknown_count: 25,
        collected_count: 125,
        items: [
          {
            ...stock,
            new_combinations: [{ id: 'a', name: '全国社保基金一零一组合', ratio: 0.5 }],
            new_shares: 1e6,
            pub_date: '2026-10-20',
          },
        ],
      },
    }),
  )
})
test('ownership search, disclosure charts and social drilldown', async ({ page }, info) => {
  await page.goto('/institution-ownership')
  await expect(page.locator('.ownership-results button')).toHaveCount(0)
  await page.getByLabel('搜索股票').fill('600001')
  await page.getByRole('button', { name: '示例科技 600001.XSHG' }).click()
  await expect(page.getByRole('heading', { name: '股东户数', exact: true })).toBeVisible()
  await expect(page.getByText('27,000')).toBeVisible()
  await expect(page.getByText('其余六类汇总十大流通股东披露股数', { exact: false })).toBeVisible()
  await expect(page.getByText('估算', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('img', { name: '公募基金持仓市值趋势（亿元）' })).toBeVisible()
  const chartData = await page.evaluate(async () => {
    // Inspect the live charts through the same ECharts module loaded by Vite.
    const echartsUrl = performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .find((url) => /\/echarts_core\.js\?/.test(url))
    if (!echartsUrl) throw new Error('ECharts module was not loaded')
    const { getInstanceByDom } = await import(echartsUrl)
    return Array.from(document.querySelectorAll('.ownership-grid .ownership-chart')).map((el) => {
      const option = getInstanceByDom(el).getOption()
      return {
        title: el.getAttribute('aria-label'),
        unit: option.yAxis[0].name,
        values: option.series[0].data,
        counts: option.series[1].data,
        tooltip: option.series[0].tooltip.valueFormatter(option.series[0].data[1]),
      }
    })
  })
  expect(chartData.map((chart) => chart.values)).toEqual([
    [10, 12],
    [100, 150],
    [200, 350],
    [300, 320],
    [400, 450],
    [500, 510],
    [null, 600],
  ])
  expect(chartData[0].unit).toContain('亿元')
  expect(chartData.slice(1).every((chart) => chart.unit.includes('万股'))).toBe(true)
  expect(chartData[2].counts).toEqual([2, 3])
  expect(chartData[2].tooltip).toBe('350 万股')
  expect(chartData[0].tooltip).toBe('12 亿元')
  await page.getByRole('button', { name: '社保新进', exact: true }).click()
  await expect(page.getByText('全国社保基金一零一组合')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '合计持股数（万股）' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '100', exact: true })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: /市值/ })).toHaveCount(0)
  await expect(page.locator('.ownership-rule')).toContainText('按新进组合合计持股数降序')
  await page.getByLabel('社保筛选口径').selectOption('combination')
  await expect(page.getByText('全国社保基金一零一组合')).toBeVisible()
  await page.getByRole('button', { name: '示例科技', exact: true }).click()
  await expect(page.getByRole('heading', { name: '股东户数', exact: true })).toBeVisible()
  const width = await page.evaluate(() => ({
    body: document.documentElement.scrollWidth,
    window: innerWidth,
  }))
  expect(width.body).toBeLessThanOrEqual(width.window + 1)
  await page.screenshot({ path: `../.runtime/ownership-${info.project.name}.png`, fullPage: true })
})
test('ownership error permits retry', async ({ page }) => {
  await page.route('**/api/v1/institution-ownership/stocks?*', (r) =>
    r.fulfill({ status: 503, json: { detail: '本地数据尚未准备好' } }),
  )
  await page.goto('/institution-ownership')
  await expect(page.locator('.ownership-results button')).toHaveCount(0)
  await page.getByLabel('搜索股票').fill('600001')
  await expect(page.getByText('本地数据尚未准备好')).toBeVisible()
  await expect(page.getByRole('button', { name: '重新加载' })).toBeVisible()
})

test('social increase shows aggregate quarter comparison', async ({ page }) => {
  await page.route('**/api/v1/institution-ownership/social-entries?*', (r) =>
    r.fulfill({
      json: {
        period: '2026-09-30',
        periods: ['2026-09-30'],
        comparable_count: 1,
        unknown_count: 0,
        collected_count: 1,
        items: [
          {
            ...stock,
            previous_shares: 1000000,
            current_shares: 1500000,
            change_shares: 500000,
            change_pct: 50,
            current_ratio: 1.9,
            pub_date: '2026-10-20',
          },
        ],
      },
    }),
  )
  await page.goto('/institution-ownership')
  await page.getByRole('button', { name: '社保新进', exact: true }).click()
  await page.getByLabel('社保筛选口径').selectOption('increase')
  await expect(page.getByRole('columnheader', { name: '上季度合计（万股）' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '150', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '50%', exact: true })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '本季度披露持股比例' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '1.9%', exact: true })).toBeVisible()
})

test('social modes use their own response and explanation when switching', async ({
  page,
}, info) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  let blockCombination = false
  let releaseCombination!: () => void
  const combinationGate = new Promise<void>((resolve) => {
    releaseCombination = resolve
  })
  let markCombinationRequested!: () => void
  const combinationRequested = new Promise<void>((resolve) => {
    markCombinationRequested = resolve
  })
  await page.route('**/api/v1/institution-ownership/social-entries?*', async (route) => {
    const mode = new URL(route.request().url()).searchParams.get('mode')
    if (mode === 'combination' && blockCombination) {
      markCombinationRequested()
      await combinationGate
    }
    const row =
      mode === 'increase'
        ? {
            ...stock,
            name: '加仓样本',
            previous_shares: 1000000,
            current_shares: 1500000,
            change_shares: 500000,
            change_pct: 50,
            current_ratio: 1.9,
          }
        : {
            ...stock,
            name: mode === 'first' ? '首次样本' : '新增样本',
            new_combinations: [{ id: 'new', name: '全国社保基金一零一组合', ratio: 0.5 }],
            new_shares: 1000000,
          }
    await route.fulfill({
      json: {
        period: '2026-09-30',
        periods: ['2026-09-30'],
        collected_count: 125,
        comparable_count: 100,
        unknown_count: 25,
        items: [row],
      },
    })
  })
  await page.goto('/institution-ownership?tab=social')
  await expect(page.getByRole('button', { name: '首次样本', exact: true })).toBeVisible()
  const mode = page.getByLabel('社保筛选口径')
  await mode.selectOption('increase')
  await expect(page.getByRole('button', { name: '加仓样本', exact: true })).toBeVisible()
  await mode.selectOption('combination')
  await expect(page.getByRole('button', { name: '新增样本', exact: true })).toBeVisible({
    timeout: 5000,
  })
  await expect(page.locator('.ownership-rule')).toContainText('本季出现上季名单中没有的社保组合')
  await expect(page.locator('.ownership-summary')).toContainText('当前匹配 1 只')
  await expect(page.getByText('仅统计公开披露', { exact: false })).toHaveCount(0)
  await page.getByLabel('筛选社保股票').fill('无匹配')
  await expect(page.locator('.ownership-summary')).toContainText('当前匹配 0 只')
  await page.getByLabel('筛选社保股票').clear()
  await mode.selectOption('first')
  await expect(page.getByRole('button', { name: '首次样本', exact: true })).toBeVisible()
  await expect(page.locator('.ownership-rule')).toContainText('上季无社保组合，本季有新进社保组合')
  await mode.selectOption('increase')
  await expect(page.getByRole('button', { name: '加仓样本', exact: true })).toBeVisible()
  await expect(page.locator('.ownership-rule')).toContainText('全部社保组合的持股总数')
  // A slow obsolete request must neither show stale rows nor replace the newest mode.
  blockCombination = true
  await mode.selectOption('combination')
  await combinationRequested
  await expect(page.getByLabel('正在加载数据')).toBeVisible()
  await expect(page.getByRole('button', { name: '加仓样本', exact: true })).toHaveCount(0)
  await mode.selectOption('first')
  await expect(page.getByRole('button', { name: '首次样本', exact: true })).toBeVisible()
  releaseCombination()
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByRole('button', { name: '首次样本', exact: true })).toBeVisible()
  expect(errors).toEqual([])
  await page.screenshot({
    path: `../.runtime/social-modes-${info.project.name}.png`,
    fullPage: true,
  })
})

test('social filters reserve layout during loading, mode switches, errors and empty results', async ({
  page,
}, info) => {
  const pending: Route[] = []
  await page.route('**/api/v1/institution-ownership/social-entries?*', (route) => {
    pending.push(route)
  })
  const nextRequest = async () => {
    await expect.poll(() => pending.length).toBeGreaterThan(0)
    // StrictMode can issue and abort an initial development-only request.
    const latest = pending.pop()!
    await Promise.all(pending.splice(0).map((route) => route.abort()))
    return latest
  }
  const response = (mode: string, count: number, period = '2026-09-30') => ({
    period,
    periods: ['2026-09-30', '2026-06-30'],
    collected_count: 5196,
    comparable_count: 4869,
    unknown_count: 327,
    items: Array.from({ length: count }, (_, i) => ({
      ...stock,
      code: `600${String(i).padStart(3, '0')}.XSHG`,
      name: `${mode}样本${i}`,
      ...(mode === 'increase'
        ? {
            previous_shares: 1000000,
            current_shares: 1500000,
            change_shares: 500000,
            change_pct: 50,
            current_ratio: 1.9,
          }
        : {
            new_combinations: [
              {
                id: 'new',
                name: '中国某某基金管理有限公司－全国社会保障基金一零一组合',
                ratio: 0.5,
              },
            ],
            new_shares: 1000000,
          }),
      pub_date: '2026-10-20',
    })),
  })
  const layout = () =>
    page.evaluate(() =>
      [
        '.ownership-summary',
        '.ownership-controls',
        '.ownership-rules',
        '.ownership-social-results',
        '[aria-label="社保报告期"]',
        '[aria-label="社保筛选口径"]',
      ].map((selector) => {
        const { x, y, width, height } = document.querySelector(selector)!.getBoundingClientRect()
        return [x + scrollX, y + scrollY, width, height]
      }),
    )
  const expectLayout = async (expected: number[][]) => {
    const actual = await layout()
    actual.forEach((box, i) =>
      box.forEach((value, j) => expect(Math.abs(value - expected[i][j])).toBeLessThanOrEqual(1)),
    )
  }
  const columns = () =>
    page
      .locator('.ownership-social-table th')
      .evaluateAll((headers) => headers.map((header) => header.getBoundingClientRect().width))
  await page.goto('/institution-ownership?tab=social')
  await page.addStyleTag({ content: '.page-enter { animation: none !important; }' })
  await page.evaluate(() => document.fonts.ready)
  const initialRequest = await nextRequest()
  const initialLayout = await layout()
  const initialColumns = await columns()
  await expect(page.locator('.ownership-loading-row')).toHaveCount(8)
  await expect(page.locator('.ownership-summary')).toContainText('已采集 — 只')
  await page.screenshot({
    path: `../.runtime/social-loading-${info.project.name}.png`,
    fullPage: true,
  })
  await initialRequest.fulfill({ json: response('first', 20) })
  await expect(page.locator('.ownership-summary')).toContainText('当前匹配 20 只')
  await expectLayout(initialLayout)
  expect(await columns()).toEqual(initialColumns)
  const results = page.getByRole('region', { name: '社保筛选结果' })
  expect(await results.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)
  const modeSelect = page.getByLabel('社保筛选口径')
  for (const mode of ['increase', 'combination']) {
    await modeSelect.selectOption(mode)
    const request = await nextRequest()
    await expect(results).toHaveAttribute('aria-busy', 'true')
    await expect(page.getByLabel('社保报告期')).toHaveValue('2026-09-30')
    await expect(page.getByLabel('社保报告期').locator('option')).toHaveCount(2)
    await expect(page.locator('.ownership-stock-link')).toHaveCount(0)
    await expectLayout(initialLayout)
    const loadingColumns = await columns()
    await request.fulfill({ json: response(mode, mode === 'increase' ? 3 : 1) })
    await expect(page.getByRole('button', { name: `${mode}样本0`, exact: true })).toBeVisible()
    await expect(results).toHaveAttribute('aria-busy', 'false')
    await expectLayout(initialLayout)
    expect(await columns()).toEqual(loadingColumns)
  }
  await page.screenshot({
    path: `../.runtime/social-stable-${info.project.name}.png`,
    fullPage: true,
  })
  await modeSelect.selectOption('first')
  await (await nextRequest()).fulfill({ status: 503, json: { detail: '测试加载失败' } })
  await expect(page.getByRole('alert')).toContainText('测试加载失败')
  await expectLayout(initialLayout)
  await page.getByRole('button', { name: '重新加载', exact: true }).click()
  const retryRequest = await nextRequest()
  await expectLayout(initialLayout)
  await retryRequest.fulfill({ json: response('first', 0) })
  await expect(page.getByText('暂无匹配记录；未披露或名单不完整不参与判定。')).toBeVisible()
  await expectLayout(initialLayout)
  await page.getByLabel('社保报告期').selectOption('2026-06-30')
  const periodRequest = await nextRequest()
  expect(new URL(periodRequest.request().url()).searchParams.get('period')).toBe('2026-06-30')
  await expectLayout(initialLayout)
  await periodRequest.fulfill({ json: response('first', 1, '2026-06-30') })
  await expect(page.getByRole('button', { name: 'first样本0', exact: true })).toBeVisible()
  await expectLayout(initialLayout)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
