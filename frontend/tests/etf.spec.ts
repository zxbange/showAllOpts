import { expect, test } from '@playwright/test'

const names = [
  '沪深300ETF华泰柏瑞',
  '沪深300ETF易方达',
  '沪深300ETF中金',
  '沪深300ETF华夏',
  '沪深300ETF工银',
]
const codes = ['510300.XSHG', '510310.XSHG', '510320.XSHG', '510330.XSHG', '510350.XSHG']
function fixture(group = 'hs300') {
  const dates = Array.from({ length: 70 }, (_, i) =>
    new Date(Date.UTC(2026, 5, 1 + i)).toISOString().slice(0, 10),
  ).filter((day) => ![0, 6].includes(new Date(day).getUTCDay()))
  const funds = names.map((name, i) => ({
    code: codes[i],
    name: group === 'hs300' ? name : name.replace('沪深300', '中证500'),
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
    group_name: group === 'hs300' ? '沪深300 ETF' : '中证500 ETF',
    start_date: dates[0],
    end_date: dates[dates.length - 1],
    baseline_date: dates[0],
    observed_days: dates.length,
    available_funds: 5,
    funds,
  }
}

test('ETF overview entry, cards, comparison, range, group and mobile layout', async ({
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
  await page.goto('/')
  await page.getByRole('link', { name: '进入ETF份额仪表盘', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText('ETF份额仪表盘', {
    timeout: 15000,
  })
  await expect(page.locator('.etf-card')).toHaveCount(5)
  await expect(page.locator('.etf-card').first()).toContainText('285.53')
  await expect(page.locator('.etf-card').nth(2)).toContainText('日变动 —')
  await expect(page.locator('.etf-card').last()).toContainText('待更新')
  await expect(page.getByRole('img', { name: 'ETF总份额曲线' }).locator('canvas')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  const nav = page.getByRole('navigation', { name: '主导航' })
  const active = nav.getByRole('link', { name: 'ETF份额仪表盘', exact: true })
  const navBox = await nav.boundingBox()
  const activeBox = await active.boundingBox()
  expect(
    navBox &&
      activeBox &&
      activeBox.x >= navBox.x - 1 &&
      activeBox.x + activeBox.width <= navBox.x + navBox.width + 1,
  ).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('etf-dashboard.png'),
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('button', { name: '份额变化率', exact: true }).click()
  await expect(page.getByRole('img', { name: 'ETF份额变化率曲线' })).toBeVisible()
  await expect(page.getByText(/共同基准日/)).toBeVisible()
  await page.getByRole('button', { name: '查看沪深300ETF华泰柏瑞曲线' }).click()
  await expect(page.getByRole('heading', { name: '份额变化率 · 沪深300ETF华泰柏瑞' })).toBeVisible()
  await page.getByRole('button', { name: '显示全部 ETF' }).click()
  await expect(page.getByRole('heading', { name: '份额变化率 · 全部' })).toBeVisible()
  await page.getByRole('button', { name: '近1月', exact: true }).click()
  await expect.poll(() => requests.at(-1)?.searchParams.get('days')).toBe('30')
  await page.getByRole('button', { name: /中证500 ETF/ }).click()
  await expect.poll(() => requests.at(-1)?.searchParams.get('group')).toBe('zz500')
  await expect(page.locator('.etf-card').first()).toContainText('中证500')
  expect(errors).toEqual([])
})

test('ETF failure retry, empty database, missing baseline and empty fund', async ({ page }) => {
  let state = 'error'
  await page.route('**/api/v1/etf-shares/history?*', (route) => {
    if (state === 'error') return route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } })
    const data = fixture()
    if (state === 'empty')
      return route.fulfill({ json: { ...data, end_date: null, observed_days: 0, funds: [] } })
    return route.fulfill({
      json: {
        ...data,
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
  await page.goto('/etf-shares')
  await expect(page.getByRole('alert')).toContainText('数据库暂不可用', { timeout: 15000 })
  state = 'empty'
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '暂无ETF份额数据' })).toBeVisible()
  state = 'partial'
  await page.getByRole('button', { name: '刷新ETF份额数据' }).click()
  await expect(page.getByRole('button', { name: '份额变化率', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '查看沪深300ETF华泰柏瑞曲线' }).click()
  await expect(page.getByRole('heading', { name: '该ETF在所选区间暂无数据' })).toBeVisible()
})
