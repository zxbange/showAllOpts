import { expect, test, adminSession, type Page } from './fixtures'
import type {
  GrowthDividendHistory,
  GrowthDividendPoint,
  GrowthDividendStatus,
} from '../src/types/growthDividend'

const status: GrowthDividendStatus = {
  running: false,
  stage: '更新完成',
  processed: 30,
  total: 30,
  percent: 100,
  last_attempt_at: '2026-11-10T18:00:00+08:00',
  last_success_at: '2026-11-10T18:00:20+08:00',
  error: null,
  pending_dates: 0,
  pending_points: 0,
  daily_time: '18:00',
  timezone: 'Asia/Shanghai',
  next_run_at: '2026-11-11T18:00:00+08:00',
}
const points: GrowthDividendPoint[] = [
  '2018-01-02',
  '2019-12-31',
  '2020-01-02',
  '2020-07-23',
  '2021-11-09',
  '2021-11-10',
  '2023-11-10',
  '2024-11-11',
  '2026-11-09',
  '2026-11-10',
].map((date, index) => ({
  date,
  chinext: 2000 + index * 100,
  dividend: 4000 + index * 150,
  star50: index < 2 || index === 4 ? null : 1000 + index * 20,
  chinext_dividend_ratio: (2000 + index * 100) / (4000 + index * 150),
  star50_dividend_ratio:
    index < 2 || index === 4 ? null : (1000 + index * 20) / (4000 + index * 150),
}))
const history: GrowthDividendHistory = {
  start_date: points[0].date,
  end_date: points.at(-1)!.date,
  latest: points.at(-1)!,
  points,
  status,
}

async function inspectChart(page: Page) {
  return page.evaluate(async () => {
    const url = performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .find((value) => /\/echarts_core\.js\?/.test(value))!
    const { getInstanceByDom } = await import(url)
    const option = getInstanceByDom(document.querySelector('.growth-dividend-chart')).getOption()
    const zoom = option.dataZoom[0]
    const dateAt = (value: number | string) =>
      typeof value === 'number' ? option.xAxis[0].data[value] : value
    return {
      start: dateAt(zoom.startValue) as string,
      end: dateAt(zoom.endValue) as string,
      startPercent: zoom.start as number,
      endPercent: zoom.end as number,
      wheel: zoom.zoomOnMouseWheel,
      slider: option.dataZoom[1].type,
      dates: option.xAxis[0].data as string[],
      selected: option.legend[0].selected as Record<string, boolean>,
      series: option.series.map(
        (line: {
          name: string
          yAxisIndex: number
          lineStyle: { type: string; color: string }
          data: Array<number | null>
          connectNulls: boolean
        }) => ({
          name: line.name,
          axis: line.yAxisIndex,
          style: line.lineStyle.type,
          color: line.lineStyle.color,
          data: line.data,
          connectNulls: line.connectNulls,
        }),
      ),
    }
  })
}

test('five-year default, all history, date entry and manual zoom survive refresh', async ({
  page,
}, info) => {
  let snapshot = history
  let reads = 0
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/growth-dividend/**', (route) => {
    if (route.request().url().endsWith('/history')) reads++
    return route.fulfill({ json: route.request().url().endsWith('/status') ? status : snapshot })
  })
  await page.goto('/growth-dividend')
  await expect(page.locator('.growth-dividend-chart canvas')).toBeVisible()
  await expect(page.getByRole('button', { name: '近5年', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  const chart = await inspectChart(page)
  // The development server mounts effects twice under React StrictMode.
  // Compare later reads to the settled initial load instead of assuming one GET.
  const initialReads = reads
  expect(chart).toMatchObject({
    start: '2021-11-10',
    end: '2026-11-10',
    slider: 'slider',
    wheel: 'ctrl',
  })
  expect(chart.dates).toHaveLength(points.length)
  expect(chart.series.map((line) => [line.name, line.axis, line.style])).toEqual([
    ['创业板指', 0, 'solid'],
    ['中证红利', 0, 'solid'],
    ['科创50', 0, 'solid'],
    ['创红比', 1, 'dashed'],
    ['科创红利比', 1, 'dashed'],
  ])
  expect(new Set(chart.series.map((line) => line.color)).size).toBe(5)
  expect(chart.series[2].data[0]).toBeNull()
  expect(chart.series[2].data[4]).toBeNull()
  expect(chart.series[3].data[0]).toBe(0.5)
  expect(chart.series.every((line) => !line.connectNulls)).toBe(true)
  await expect(page.getByText('每个交易日北京时间 18:00 更新')).toBeVisible()
  await page.getByRole('button', { name: '全部', exact: true }).click()
  await expect
    .poll(() => inspectChart(page))
    .toMatchObject({ start: '2018-01-02', end: '2026-11-10' })
  await page.getByRole('button', { name: '隐藏科创50', exact: true }).click()
  await expect.poll(async () => (await inspectChart(page)).selected['科创50']).toBe(false)
  await page.getByRole('button', { name: '显示科创50', exact: true }).click()
  await expect.poll(async () => (await inspectChart(page)).selected['科创50']).toBe(true)
  await page.getByLabel('创红比开始日期', { exact: true }).fill('2020-01-01')
  await page.getByLabel('创红比结束日期', { exact: true }).fill('2021-11-10')
  await page.getByRole('button', { name: '应用', exact: true }).click()
  await expect
    .poll(() => inspectChart(page))
    .toMatchObject({ start: '2020-01-02', end: '2021-11-10' })
  await page.evaluate(async () => {
    const url = performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .find((value) => /\/echarts_core\.js\?/.test(value))!
    const { getInstanceByDom } = await import(url)
    getInstanceByDom(document.querySelector('.growth-dividend-chart')).dispatchAction({
      type: 'dataZoom',
      start: 40,
      end: 80,
    })
  })
  const manual = await inspectChart(page)
  await expect(page.getByRole('button', { name: '近5年', exact: true })).toHaveAttribute(
    'aria-pressed',
    'false',
  )
  expect(reads).toBe(initialReads)
  snapshot = {
    ...history,
    end_date: '2026-11-11',
    latest: { ...points.at(-1)!, date: '2026-11-11' },
    points: [...points, { ...points.at(-1)!, date: '2026-11-11' }],
  }
  await page.getByRole('button', { name: '刷新创红比展示数据' }).click()
  await expect(page.locator('.date-badge')).toContainText('2026-11-11')
  await expect
    .poll(() => inspectChart(page))
    .toMatchObject({ start: manual.start, end: manual.end })
  expect(reads).toBe(initialReads + 1)
  await page.getByRole('button', { name: '近5年', exact: true }).click()
  await expect
    .poll(() => inspectChart(page))
    .toMatchObject({ start: '2023-11-10', end: '2026-11-11' })
  // Today's entirely missing row is visible in the full calendar but must not
  // become the end of the default five-year window or invalidate the date form.
  snapshot = {
    ...snapshot,
    points: [
      ...snapshot.points,
      {
        date: '2026-11-12',
        chinext: null,
        dividend: null,
        star50: null,
        chinext_dividend_ratio: null,
        star50_dividend_ratio: null,
      },
    ],
  }
  await page.getByRole('button', { name: '刷新创红比展示数据' }).click()
  const endInput = page.getByLabel('创红比结束日期', { exact: true })
  await expect(endInput).toHaveAttribute('max', '2026-11-12')
  await expect(endInput).toHaveValue('2026-11-11')
  await expect.poll(() => inspectChart(page)).toMatchObject({ end: '2026-11-11' })
  await page.getByRole('button', { name: '全部', exact: true }).click()
  await expect(endInput).toHaveValue('2026-11-12')
  expect(await endInput.evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(true)
  await page.getByRole('button', { name: '应用', exact: true }).click()
  await expect
    .poll(() => inspectChart(page))
    .toMatchObject({ start: '2018-01-02', end: '2026-11-12' })
  await page.screenshot({
    path: info.outputPath('growth-dividend.png'),
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(errors).toEqual([])
})

test('initial backfill, update progress and failed gaps retain existing curves', async ({
  page,
}) => {
  let current: GrowthDividendStatus = {
    ...status,
    running: true,
    processed: 0,
    total: 50,
    percent: null,
    stage: '正在补齐2018年以来的指数历史',
    last_success_at: null,
  }
  let snapshot: GrowthDividendHistory = {
    ...history,
    latest: null,
    points: [],
    start_date: null,
    end_date: null,
    status: current,
  }
  let posts = 0
  await page.route('**/api/v1/growth-dividend/**', (route) => {
    const action = new URL(route.request().url()).pathname.split('/').at(-1)
    if (route.request().method() === 'POST') {
      posts++
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      current = { ...status, running: true, percent: 20, stage: '正在补齐缺失数据' }
      return route.fulfill({ json: { started: true } })
    }
    return route.fulfill({ json: action === 'status' ? current : { ...snapshot, status: current } })
  })
  await page.goto('/growth-dividend')
  await expect(
    page.getByRole('heading', { name: '正在补齐指数历史数据', exact: true }),
  ).toBeVisible()
  await expect(page.getByRole('progressbar', { name: '创红比更新进度' })).toBeVisible()
  current = {
    ...status,
    running: false,
    stage: '部分数据待补',
    error: '公开行情接口暂时不可用',
    pending_dates: 3,
    pending_points: 4,
  }
  snapshot = { ...history, status: current }
  await expect(page.locator('.growth-dividend-chart canvas')).toBeVisible({ timeout: 6000 })
  await expect(page.locator('.growth-dividend-pending')).toHaveText(
    '待补 3 个交易日 · 4 条指数数据',
  )
  await expect(page.getByRole('alert')).toContainText('已保留现有数据')
  await page.getByRole('button', { name: '更新数据', exact: true }).click()
  await expect(page.getByRole('progressbar', { name: '创红比更新进度' })).toHaveAttribute(
    'value',
    '20',
  )
  await expect(page.locator('.growth-dividend-chart canvas')).toBeVisible()
  expect(posts).toBe(1)
})

test('new tab stays inside market sentiment and read-only accounts cannot trigger collection', async ({
  page,
}) => {
  await page.route('**/api/auth/**', (route) =>
    route.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } } }),
  )
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线导航测试' } }),
  )
  let posts = 0
  await page.route('**/api/v1/growth-dividend/**', (route) => {
    if (route.request().method() === 'POST') posts++
    return route.fulfill({ json: route.request().url().endsWith('/status') ? status : history })
  })
  await page.goto('/market-sentiment')
  const tabs = page.getByRole('navigation', { name: '市场情绪模块切换' })
  await expect(tabs.getByRole('link')).toHaveText([
    '国家队动态',
    '抱团拥挤度',
    '市场情绪跟踪',
    '两融担保比例',
    '创红比',
  ])
  await tabs.getByRole('link', { name: '创红比', exact: true }).click()
  await expect(page).toHaveURL(/growth-dividend$/)
  await expect(tabs.getByRole('link', { name: '创红比', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.getByRole('button', { name: '更新数据', exact: true })).toBeDisabled()
  await expect(page.locator('.growth-dividend-chart canvas')).toBeVisible()
  expect(posts).toBe(0)
})

test('duplicate clicks send one command and a failed update remains visible after successful reads', async ({
  page,
}) => {
  let posts = 0
  let fail = true
  await page.route('**/api/v1/growth-dividend/**', async (route) => {
    if (route.request().method() === 'POST') {
      posts++
      await new Promise((resolve) => setTimeout(resolve, 100))
      return fail
        ? route.fulfill({ status: 503, json: { detail: '本次更新未能启动，请重试' } })
        : route.fulfill({ json: { started: true } })
    }
    return route.fulfill({ json: route.request().url().endsWith('/status') ? status : history })
  })
  await page.goto('/growth-dividend')
  const update = page.getByRole('button', { name: '更新数据', exact: true })
  await expect(update).toBeEnabled()
  await update.evaluate((element: HTMLButtonElement) => {
    element.click()
    element.click()
  })
  await expect(page.getByRole('alert')).toContainText('本次更新未能启动')
  expect(posts).toBe(1)
  await page.getByRole('button', { name: '刷新创红比展示数据' }).click()
  await expect(page.getByRole('button', { name: '刷新创红比展示数据' })).toBeEnabled()
  await expect(page.getByRole('alert')).toContainText('本次更新未能启动')
  await expect(page.locator('.growth-dividend-chart canvas')).toBeVisible()
  fail = false
  await update.click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(posts).toBe(2)
})
