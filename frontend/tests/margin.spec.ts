import { expect, test } from '@playwright/test'
import type { MarginHistory, MarginStatus } from '../src/types/margin'

const status: MarginStatus = {
  running: false,
  cancelling: false,
  stage: '等待更新',
  processed: 0,
  total: null,
  percent: null,
  last_attempt_at: null,
  last_success_at: null,
  error: null,
  daily_time: '20:00',
  timezone: 'Asia/Shanghai',
  next_run_at: '2026-10-28T20:00:00+08:00',
}
const points = [222, 303.51, 257.6, 255.6273].map((ratio, index) => ({
  date: ['2020-10-27', '2026-05-13', '2026-10-23', '2026-10-26'][index],
  ratio,
  source_url: 'https://www.csdata.cn/cmsmc/articleFileDir/2026-10/26/test.xlsx',
}))
const history: MarginHistory = {
  start_date: points[0].date,
  end_date: points[3].date,
  latest: { ...points[3], updated_at: '2026-10-27T20:00:01+08:00' },
  points,
  status,
  summary: {
    observed_days: 4,
    change_pp: -1.9727,
    percentile: 25,
    below_count: 1,
    low_ratio: 222,
    low_date: '2020-10-27',
    peak_ratio: 303.51,
    peak_date: '2026-05-13',
    distance_pp: 15.6273,
  },
}
const empty: MarginHistory = {
  ...history,
  latest: null,
  points: [],
  start_date: null,
  end_date: null,
  summary: {
    observed_days: 0,
    change_pp: null,
    percentile: null,
    below_count: 0,
    low_ratio: null,
    low_date: null,
    peak_ratio: null,
    peak_date: null,
    distance_pp: null,
  },
}

test('market mood third tab renders official percentage units and keeps chart during reload', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  let delay = false
  let mutations = 0
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.route('**/api/v1/margin-guarantee/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'POST') mutations++
    if (path.endsWith('/history') && delay) await new Promise((resolve) => setTimeout(resolve, 600))
    return route.fulfill({ json: path.endsWith('/status') ? status : history })
  })
  await page.goto('/market-crowding')
  const tabs = page.getByRole('navigation', { name: '市场情绪模块切换' })
  await expect(tabs.getByRole('link')).toHaveCount(3)
  await tabs.getByRole('link', { name: '两融担保比例', exact: true }).click()
  await expect(page.locator('.margin-stats')).toContainText('255.63%')
  await expect(page.locator('.margin-stats')).toContainText('-1.97 个百分点')
  await expect(page.locator('.margin-stats')).toContainText('25.0%')
  await expect(page.locator('.date-badge')).toContainText('2026-10-26')
  await expect(page.getByText('中证数据 · 每天北京时间 20:00 检查更新')).toBeVisible()
  await expect(page.locator('.margin-chart canvas')).toBeVisible()
  await expect(page.getByRole('link', { name: '查看官方原表' })).toHaveAttribute(
    'href',
    points[3].source_url,
  )
  const documentTop = () =>
    page
      .locator('.margin-trend')
      .evaluate((element) => element.getBoundingClientRect().top + window.scrollY)
  // The bounding rect includes the page's entrance transform; settle it before the reload baseline.
  await page.locator('.margin-page').evaluate((element) =>
    Promise.all(
      element
        .getAnimations()
        .filter(
          (animation) => animation instanceof CSSAnimation && animation.animationName === 'page-in',
        )
        .map((animation) => animation.finished),
    ),
  )
  const top = await documentTop()
  delay = true
  await page.getByRole('button', { name: '刷新两融展示数据' }).click()
  await expect(page.locator('.margin-trend')).toHaveAttribute('aria-busy', 'true')
  await expect(page.locator('.margin-chart canvas')).toBeVisible()
  expect(Math.abs((await documentTop()) - top)).toBeLessThan(1)
  await expect(page.locator('.margin-trend')).toHaveAttribute('aria-busy', 'false')
  await page.screenshot({
    path: testInfo.outputPath('margin-guarantee.png'),
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(mutations).toBe(0)
  expect(errors).toEqual([])
})

test('empty history starts explicitly and shows progress with stop only while running', async ({
  page,
}) => {
  let current: MarginStatus = { ...status }
  let starts = 0
  let stops = 0
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.route('**/api/v1/margin-guarantee/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/refresh')) {
      starts++
      current = {
        ...status,
        running: true,
        stage: '读取官方报表',
        processed: 42,
        total: 120,
        percent: 35,
      }
      return route.fulfill({ json: { started: true } })
    }
    if (path.endsWith('/cancel')) {
      stops++
      current = { ...current, running: false, stage: '已停止，已完成记录保留' }
      return route.fulfill({ json: { cancelled: true } })
    }
    return route.fulfill({
      json: path.endsWith('/status') ? current : { ...empty, status: current },
    })
  })
  await page.goto('/margin-guarantee')
  await expect(page.getByRole('heading', { name: '暂无两融担保比例数据' })).toBeVisible()
  await expect(page.getByRole('button', { name: '停止采集' })).toHaveCount(0)
  expect(starts).toBe(0)
  await page.getByRole('button', { name: '采集历史数据' }).click()
  await expect(page.getByRole('progressbar', { name: '两融采集进度' })).toHaveAttribute(
    'value',
    '35',
  )
  await expect(page.locator('.margin-progress-label')).toContainText('42 / 120')
  await page.getByRole('button', { name: '停止采集' }).click()
  await expect(page.getByRole('button', { name: '停止采集' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '采集历史数据' })).toBeVisible()
  expect(starts).toBe(1)
  expect(stops).toBe(1)
})

test('failed source preserves previous chart and a new collection clears failure', async ({
  page,
}) => {
  let current: MarginStatus = {
    ...status,
    error:
      '连续三份日报读取失败，已停止本轮。日报 2026-10-26：中证服务器返回 HTTP 403。 [http_status/HTTPStatusError] 请稍后重试；再次更新会继续补采。',
    stage: '更新失败',
  }
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.route('**/api/v1/margin-guarantee/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/refresh')) {
      current = { ...status, running: true, stage: '读取目录', percent: null }
      return route.fulfill({ json: { started: true } })
    }
    return route.fulfill({
      json: path.endsWith('/status') ? current : { ...history, status: current },
    })
  })
  await page.goto('/margin-guarantee')
  await expect(page.getByRole('alert')).toContainText(
    '日报 2026-10-26：中证服务器返回 HTTP 403。 [http_status/HTTPStatusError]',
  )
  await expect(page.locator('.margin-chart canvas')).toBeVisible()
  await page.getByRole('button', { name: '更新数据', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.locator('.margin-chart canvas')).toBeVisible()
  await expect(page.getByRole('progressbar')).not.toHaveAttribute('value')
  await expect(page.getByRole('navigation', { name: '市场情绪模块切换' })).toBeVisible()
})

test('first saved record appears during backfill and later history reads are throttled', async ({
  page,
}) => {
  let processed = 0
  let historyReads = 0
  let concurrentHistory = 0
  let peakConcurrent = 0
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.route('**/api/v1/margin-guarantee/**', async (route) => {
    const current: MarginStatus = {
      ...status,
      running: true,
      stage: '读取官方报表',
      processed,
      total: 120,
      percent: (processed / 120) * 100,
    }
    if (new URL(route.request().url()).pathname.endsWith('/status'))
      return route.fulfill({ json: current })
    historyReads++
    concurrentHistory++
    peakConcurrent = Math.max(peakConcurrent, concurrentHistory)
    await new Promise((resolve) => setTimeout(resolve, 150))
    concurrentHistory--
    return route.fulfill({ json: { ...(processed ? history : empty), status: current } })
  })
  await page.goto('/margin-guarantee')
  await expect(page.getByRole('heading', { name: '暂无两融担保比例数据' })).toBeVisible()
  // StrictMode aborts its first mount request; only measure live backfill polling.
  peakConcurrent = concurrentHistory
  processed = 1
  await expect(page.locator('.margin-chart canvas')).toBeVisible({ timeout: 10000 })
  await expect(page.getByRole('button', { name: '停止采集' })).toBeVisible()
  const reads = historyReads
  processed = 2
  await expect(page.locator('.margin-progress-label')).toContainText('2 / 120', { timeout: 10000 })
  expect(historyReads).toBe(reads)
  expect(peakConcurrent).toBe(1)
})
