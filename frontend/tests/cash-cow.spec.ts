import { test, expect, type Page } from './fixtures'
import type { CashCowOverview, CashCowStatus } from '../src/types/cashCow'
const idle: CashCowStatus = {
  periods: [3, 5, 10],
  collection_years: 10,
  run_id: null,
  running: false,
  cancelling: false,
  stage: '等待更新',
  processed: 0,
  total: null,
  last_attempt_at: null,
  last_success_at: null,
  error: null,
}
const stock = {
  code: '600001',
  name: '测试股份',
  exchange: 'SH',
  current_yield: 5,
  latest_price: 9,
  latest_price_date: '2026-11-09',
  dividend_ttm_per_share: 0.45,
  average_yield: 4.5,
  payout_min: 35,
  payout_max: 60,
  coverage_min: 1.5,
  years: Array.from({ length: 10 }, (_, i) => ({
    year: 2016 + i,
    dividend_per_share: 0.45,
    cash_dividend: 1e8,
    adjusted_profit: 2e8,
    net_profit: 2.5e8,
    operating_cashflow: 3e8,
    year_average_close: 10,
    price_date: `${2016 + i}-12-30`,
    payout_ratio: 40,
    cashflow_coverage: 3,
    dividend_yield: 4.5,
  })),
}
const saved: CashCowOverview = {
  snapshot: {
    as_of: '2026-11-10',
    updated_at: '2026-11-10T10:00:00+08:00',
    start_year: 2016,
    end_year: 2025,
    universe_count: 100,
    evaluated_count: 90,
    excluded_count: 89,
    insufficient_count: 10,
    matched_count: 1,
    source: '公开接口',
  },
  items: [stock],
  status: idle,
}
async function mock(page: Page, payload: CashCowOverview) {
  await page.route('**/api/v1/**', (route) => route.fulfill({ status: 503, json: {} }))
  await page.route('**/api/v1/cash-cow/latest?years=10', (route) =>
    route.fulfill({ json: payload }),
  )
}

test('empty page is read only until requested and has six navigation modules', async ({ page }) => {
  await mock(page, { snapshot: null, items: [], status: idle })
  const writes: string[] = [],
    polls: string[] = []
  page.on('request', (req) => {
    if (req.method() === 'POST') writes.push(req.url())
    if (new URL(req.url()).pathname.endsWith('/cash-cow/status')) polls.push(req.url())
  })
  await page.goto('/cash-cow')
  await expect(page.getByRole('heading', { name: '暂无筛选快照' })).toBeVisible()
  await expect(page.getByLabel('成功更新时间')).toContainText('最近成功更新 —')
  await expect(page.getByLabel('成功更新时间')).toContainText('上上一次 —')
  await expect(page.getByRole('button', { name: '更新全部周期' })).toBeEnabled()
  await expect(
    page.getByRole('navigation', { name: '业绩选股模块切换' }).getByRole('link'),
  ).toHaveCount(6)
  await page.waitForTimeout(2200)
  expect(writes).toEqual([])
  expect(polls).toEqual([])
})

test('search and ten annual rows work without overflowing mobile viewport', async ({ page }) => {
  await mock(page, saved)
  await page.goto('/cash-cow')
  await expect(page.getByText('测试股份', { exact: true })).toBeVisible()
  await page.getByLabel('搜索入选股票').fill('没有')
  await expect(page.getByText('没有匹配的入选股票', { exact: true })).toBeVisible()
  await page.getByLabel('搜索入选股票').fill('600001')
  await page.locator('.cash-cow-stock summary').click()
  await expect(page.locator('.cash-cow-table tbody tr')).toHaveCount(10)
  await expect(page.locator('.cash-cow-table')).toContainText('2016-12-30')
  await expect(page.locator('.cash-cow-table')).toContainText('4.50%')
  await expect(page.locator('.cash-cow-table')).toContainText('年度股息率')
  await expect(page.locator('.cash-cow-table')).toContainText('股利支付率')
  await expect(page.locator('.cash-cow-table')).toContainText('年均收盘价')
  await expect(page.locator('.cash-cow-stock summary')).toContainText('5.00%')
  await expect(page.locator('.cash-cow-counts')).toContainText('待核验 10')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

for (const insufficient of [0, 10])
  test(`zero matches distinguish ${insufficient} incomplete stocks`, async ({ page }) => {
    await mock(page, {
      ...saved,
      items: [],
      snapshot: { ...saved.snapshot!, insufficient_count: insufficient, matched_count: 0 },
    })
    await page.goto('/cash-cow')
    await expect(
      page.getByRole('heading', {
        name: insufficient ? '已判定股票中暂无入选' : '本次筛选无符合条件的股票',
      }),
    ).toBeVisible()
    if (insufficient)
      await expect(page.getByText('另有 10 只资料不足，尚不能判断是否符合。')).toBeVisible()
  })

test('refresh and cancel are single-flight and preserve published results', async ({ page }) => {
  let status = idle,
    starts = 0,
    cancels = 0
  await mock(page, saved)
  await page.route('**/api/v1/cash-cow/latest?years=10', (route) =>
    route.fulfill({ json: { ...saved, status } }),
  )
  await page.route('**/api/v1/cash-cow/status?years=10', (route) => route.fulfill({ json: status }))
  await page.route('**/api/v1/cash-cow/refresh', async (route) => {
    starts++
    await new Promise((resolve) => setTimeout(resolve, 250))
    status = { ...idle, running: true, total: 100, processed: 2, stage: '采集中' }
    await route.fulfill({ json: { started: true } })
  })
  await page.route('**/api/v1/cash-cow/cancel', async (route) => {
    cancels++
    await new Promise((resolve) => setTimeout(resolve, 250))
    status = { ...status, cancelling: true }
    await route.fulfill({ json: { cancelled: true } })
  })
  await page.goto('/cash-cow')
  await expect(page.getByRole('button', { name: '更新全部周期' })).toBeEnabled()
  await page.getByRole('button', { name: '更新全部周期' }).evaluate((el: HTMLButtonElement) => {
    el.click()
    el.click()
  })
  await expect(page.getByRole('button', { name: '停止采集' })).toBeEnabled()
  expect(starts).toBe(1)
  await page.getByRole('button', { name: '停止采集' }).evaluate((el: HTMLButtonElement) => {
    el.click()
    el.click()
  })
  await expect(page.getByRole('button', { name: '正在停止…' })).toBeDisabled()
  expect(cancels).toBe(1)
  status = { ...idle, stage: '已停止' }
  await expect(page.getByRole('button', { name: '更新全部周期' })).toBeEnabled({ timeout: 7000 })
  await expect(page.getByText('测试股份', { exact: true })).toBeVisible()
  await expect(
    page.getByText('当前仍展示上次成功快照；失败或停止不会覆盖已保存结果。'),
  ).toBeVisible()
})

test('failed collection retains prior snapshot and read failure is retryable', async ({ page }) => {
  await mock(page, { ...saved, status: { ...idle, error: '公开接口暂不可用', stage: '采集失败' } })
  await page.goto('/cash-cow')
  await expect(page.getByRole('alert')).toContainText('已保留上次成功结果')
  await expect(page.getByText('测试股份', { exact: true })).toBeVisible()
  await page.route('**/api/v1/cash-cow/refresh', (route) =>
    route.fulfill({ status: 503, json: { detail: '启动失败' } }),
  )
  await page.getByRole('button', { name: '更新全部周期' }).click()
  await expect(page.getByRole('alert')).toContainText('启动失败')
  await expect(page.getByText('测试股份', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '重新读取' }).click()
  await expect(page.getByRole('alert')).toContainText('公开接口暂不可用')
})

test('missing current yield is shown as unavailable, not zero', async ({ page }) => {
  await mock(page, {
    ...saved,
    items: [
      {
        ...stock,
        current_yield: null,
        latest_price: null,
        latest_price_date: null,
        dividend_ttm_per_share: null,
      },
    ],
  })
  await page.goto('/cash-cow')
  await expect(page.locator('.cash-cow-stock summary')).toContainText('当前股息率（TTM）—')
  await expect(page.locator('.cash-cow-stock summary [title="当前数据待核验"]')).not.toContainText(
    '0.00%',
  )
})

function periodData(years: number): CashCowOverview {
  return {
    ...saved,
    snapshot: { ...saved.snapshot!, start_year: 2026 - years },
    items: [{ ...stock, name: `${years}年股份`, years: stock.years.slice(-years) }],
  }
}

test('period selector follows URL and shows matching annual rows and definitions', async ({
  page,
}) => {
  await mock(page, saved)
  await page.route('**/api/v1/cash-cow/latest?years=*', (route) =>
    route.fulfill({
      json: periodData(Number(new URL(route.request().url()).searchParams.get('years'))),
    }),
  )
  await page.goto('/cash-cow?years=invalid&keep=yes')
  await expect(page).toHaveURL(/years=10&keep=yes/)
  for (const years of [10, 3, 5]) {
    await page.getByLabel('筛选年限').selectOption(String(years))
    await expect(page).toHaveURL(new RegExp(`years=${years}&keep=yes`))
    await expect(page.getByText(`${years}年股份`, { exact: true })).toBeVisible()
    await page.locator('.cash-cow-stock summary').click()
    await expect(page.locator('.cash-cow-table tbody tr')).toHaveCount(years)
    await page.locator('.cash-cow-rules').evaluate((el: HTMLDetailsElement) => {
      el.open = true
    })
    await expect(page.locator('.cash-cow-rules')).toContainText(`${years} 个完整年度`)
    await expect(page.locator('.cash-cow-rules')).toContainText(
      '每月最后一天 21:00 更新（北京时间）',
    )
  }
  await page.goBack()
  await expect(page.getByLabel('筛选年限')).toHaveValue('3')
  await expect(page.getByText('3年股份', { exact: true })).toBeVisible()
})

test('period snapshots distinguish uncollected from genuine zero results', async ({ page }) => {
  await mock(page, saved)
  await page.route('**/api/v1/cash-cow/latest?years=3', (route) =>
    route.fulfill({ json: { snapshot: null, items: [], status: idle } }),
  )
  await page.route('**/api/v1/cash-cow/latest?years=5', (route) =>
    route.fulfill({
      json: {
        ...saved,
        items: [],
        snapshot: { ...saved.snapshot!, matched_count: 0, insufficient_count: 0 },
      },
    }),
  )
  await page.goto('/cash-cow?years=3')
  await expect(page.getByRole('heading', { name: '暂无筛选快照' })).toBeVisible()
  await page.getByLabel('筛选年限').selectOption('5')
  await expect(page.getByRole('heading', { name: '本次筛选无符合条件的股票' })).toBeVisible()
  await page.getByLabel('筛选年限').selectOption('10')
  await expect(page.getByText('测试股份', { exact: true })).toBeVisible()
})

test('switching periods aborts old polling and ignores delayed snapshots without cancelling jobs', async ({
  page,
}) => {
  let polls = 0
  const writes: string[] = []
  await mock(page, { ...saved, status: { ...idle, running: true, stage: '全部周期采集中' } })
  await page.route('**/api/v1/cash-cow/status?years=10', async (route) => {
    polls++
    await new Promise((resolve) => setTimeout(resolve, 1500))
    await route.fulfill({ json: { ...idle, running: true, stage: '旧状态不应显示' } })
  })
  await page.route('**/api/v1/cash-cow/latest?years=3', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1000))
    await route.fulfill({ json: periodData(3) })
  })
  await page.route('**/api/v1/cash-cow/latest?years=5', (route) =>
    route.fulfill({
      json: {
        ...periodData(5),
        status: { ...idle, running: true, stage: '全部周期采集中', run_id: 'shared-run' },
      },
    }),
  )
  await page.route('**/api/v1/cash-cow/status?years=5', (route) =>
    route.fulfill({
      json: { ...idle, running: true, stage: '全部周期采集中', run_id: 'shared-run' },
    }),
  )
  page.on('request', (request) => {
    if (request.method() === 'POST') writes.push(request.url())
  })
  await page.goto('/cash-cow')
  await expect(page.getByRole('button', { name: '停止采集' })).toBeEnabled()
  await expect.poll(() => polls).toBeGreaterThan(0)
  await page.getByLabel('筛选年限').selectOption('3')
  await page.getByLabel('筛选年限').selectOption('5')
  await expect(page.getByText('5年股份', { exact: true })).toBeVisible()
  await page.waitForTimeout(2600)
  await expect(page.getByText('5年股份', { exact: true })).toBeVisible()
  await expect(page.getByText('旧状态不应显示', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '停止采集' })).toBeEnabled()
  expect(polls).toBe(1)
  expect(writes).toEqual([])
})

for (const historyCount of [0, 1, 2])
  test(`successful update history displays ${historyCount} prior timestamps in China time`, async ({
    page,
  }) => {
    await mock(page, {
      ...saved,
      snapshot: {
        ...saved.snapshot!,
        updated_at: '2026-10-31T13:00:00Z',
        previous_updated_at: historyCount >= 1 ? '2026-09-30T13:00:00Z' : null,
        previous_previous_updated_at: historyCount >= 2 ? '2026-08-31T13:00:00Z' : null,
      },
    })
    await page.goto('/cash-cow')
    const history = page.getByLabel('成功更新时间')
    await expect(history).toContainText('最近成功更新 2026/10/31 21:00:00')
    await expect(history).toContainText(
      historyCount >= 1 ? '上一次 2026/9/30 21:00:00' : '上一次 —',
    )
    await expect(history).toContainText(
      historyCount >= 2 ? '上上一次 2026/8/31 21:00:00' : '上上一次 —',
    )
  })

test('unified successful timestamps match across periods and remain unchanged on failed runs', async ({
  page,
}) => {
  const historic = {
    ...saved.snapshot!,
    updated_at: '2026-10-31T13:00:00Z',
    previous_updated_at: '2026-09-30T13:00:00Z',
    previous_previous_updated_at: '2026-08-31T13:00:00Z',
  }
  await mock(page, {
    ...saved,
    snapshot: historic,
    status: {
      ...idle,
      stage: '更新失败',
      error: '来源暂不可用',
      last_attempt_at: '2026-11-30T13:00:00Z',
    },
  })
  await page.route('**/api/v1/cash-cow/latest?years=3', (route) =>
    route.fulfill({
      json: {
        ...periodData(3),
        snapshot: {
          ...historic,
          updated_at: '2026-10-31T13:00:00Z',
        },
        status: { ...idle, stage: '已取消，保留上次结果', last_attempt_at: '2026-11-30T13:00:00Z' },
      },
    }),
  )
  await page.goto('/cash-cow')
  const history = page.getByLabel('成功更新时间')
  await expect(history).toContainText('上上一次 2026/8/31 21:00:00')
  await expect(history).not.toContainText('2026/11/30')
  await page.getByLabel('筛选年限').selectOption('3')
  await expect(history).toContainText('最近成功更新 2026/10/31 21:00:00')
  await expect(history).toContainText('上一次 2026/9/30 21:00:00')
  await expect(history).toContainText('上上一次 2026/8/31 21:00:00')
  await expect(history).not.toContainText('2026/11/30')
  await page.getByLabel('筛选年限').selectOption('10')
  await expect(history).toContainText('最近成功更新 2026/10/31 21:00:00')
  await expect(history).toContainText('上上一次 2026/8/31 21:00:00')
})

test('switching views during a pending refresh keeps one command and shares cancellation', async ({
  page,
}) => {
  let status = idle
  let starts = 0
  let cancels = 0
  let finishStart!: () => void
  const pending = new Promise<void>((resolve) => {
    finishStart = resolve
  })
  await mock(page, saved)
  await page.route('**/api/v1/cash-cow/latest?years=*', (route) =>
    route.fulfill({
      json: {
        ...periodData(Number(new URL(route.request().url()).searchParams.get('years'))),
        status,
      },
    }),
  )
  await page.route('**/api/v1/cash-cow/status?years=*', (route) => route.fulfill({ json: status }))
  await page.route('**/api/v1/cash-cow/refresh', async (route) => {
    starts++
    await pending
    status = { ...idle, running: true, stage: '全部周期采集中', run_id: 'one-run' }
    await route.fulfill({ json: { started: true } })
  })
  await page.route('**/api/v1/cash-cow/cancel', (route) => {
    cancels++
    status = { ...status, cancelling: true }
    return route.fulfill({ json: { cancelled: true } })
  })
  await page.goto('/cash-cow?years=3')
  await page.getByRole('button', { name: '更新全部周期' }).click()
  await expect.poll(() => starts).toBe(1)
  await page.getByLabel('筛选年限').selectOption('5')
  await expect(page.getByText('5年股份', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '正在提交…' })).toBeDisabled()
  await page.getByRole('button', { name: '正在提交…' }).evaluate((el: HTMLButtonElement) => {
    el.click()
    el.click()
  })
  await page.getByLabel('筛选年限').selectOption('10')
  await expect(page.getByRole('button', { name: '正在提交…' })).toBeDisabled()
  finishStart()
  await expect(page.getByRole('button', { name: '停止采集' })).toBeEnabled()
  expect(starts).toBe(1)
  await page.getByLabel('筛选年限').selectOption('3')
  await page.getByRole('button', { name: '停止采集' }).click()
  await expect(page.getByRole('button', { name: '正在停止…' })).toBeDisabled()
  await page.getByLabel('筛选年限').selectOption('5')
  await expect(page.getByRole('button', { name: '正在停止…' })).toBeDisabled()
  expect(starts).toBe(1)
  expect(cancels).toBe(1)
})
