import { readFileSync } from 'node:fs'
import { test, expect } from './fixtures'
const seed = JSON.parse(readFileSync('../backend/app/data/cci_coal.json', 'utf8'))
const data = {
  rows: seed.rows,
  missingChanges: 238,
  status: { running: false, stage: '采集完成', checkedAt: '2026-11-03T01:27:20Z' },
}

test('CCI peer module, gaps, grade selection, schedule, responsive navigation', async ({
  page,
}, info) => {
  let calls = 0
  await page.route('**/api/v1/commodities/cci**', async (route) => {
    if (route.request().method() === 'POST') {
      calls++
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      return route.fulfill({ json: { started: true } })
    }
    return route.fulfill({ json: data })
  })
  await page.route('**/api/v1/commodities/a00', (route) =>
    route.fulfill({ json: { rows: [], status: { running: false, automatic: true } } }),
  )
  await page.goto('/commodities?instrument=cci')
  await expect(page.getByRole('heading', { name: 'CCI动力煤', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'CCI动力煤', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.locator('.commodity-chart canvas')).toBeVisible()
  await expect(page.getByText(/每天 18:00 自动采集/)).toBeVisible()
  await expect(page.getByText(/11:00/)).toHaveCount(0)
  await expect(page.getByRole('row')).toHaveCount(587)
  await page.getByLabel('CCI报价档位').selectOption('CCI5000')
  await expect(page.getByRole('row')).toHaveCount(196)
  await expect(page.getByRole('cell', { name: '2026-01-21', exact: true })).toHaveCount(0)
  await expect(page.getByRole('cell', { name: '—', exact: true }).first()).toBeVisible()
  await page.getByLabel('CCI报价档位').selectOption('all')
  await page.getByRole('button', { name: '更新今日' }).click()
  expect(calls).toBe(1)
  await expect(page.getByRole('button', { name: '更新今日' })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    window.scrollTo(0, 0)
  })
  await page.screenshot({ path: info.outputPath('cci.png'), fullPage: true })
  await page.getByRole('link', { name: 'A00铝', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'A00铝', exact: true })).toBeVisible()
  await expect(page.getByText(/每天 11:00、18:00/)).toBeVisible()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'CCI动力煤', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'CCI动力煤', exact: true })).toBeVisible()
})

test('CCI failed refresh retains displayed history and prevents repeated start', async ({
  page,
}) => {
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  let posts = 0
  await page.route('**/api/v1/commodities/cci**', async (route) => {
    if (route.request().method() === 'POST') {
      posts++
      await pending
      return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
    }
    return route.fulfill({ json: data })
  })
  await page.goto('/commodities?instrument=cci')
  await page.getByRole('button', { name: '更新今日' }).click()
  await expect(page.getByRole('button', { name: '更新中…' })).toBeDisabled()
  expect(posts).toBe(1)
  release()
  await expect(page.getByRole('alert')).toContainText('CCI更新未启动')
  await expect(page.getByRole('row')).toHaveCount(587)
})

for (const outcome of ['empty', 'partial', 'failed', 'completed']) {
  test(`CCI today status ${outcome} preserves history`, async ({ page }) => {
    const hits = outcome === 'completed' ? 3 : outcome === 'partial' ? 1 : 0
    const stage =
      outcome === 'empty'
        ? '来源无当日报价，历史保留'
        : outcome === 'failed'
          ? '当日报价读取失败，历史保留'
          : outcome === 'partial'
            ? '当日报价部分可用'
            : '当日三档报价已核验'
    await page.route('**/api/v1/commodities/cci', (route) =>
      route.fulfill({
        json: {
          ...data,
          status: {
            ...data.status,
            stage,
            targetDate: '2026-11-03',
            observed: hits,
            inserted: hits,
            changed: 0,
            grades: ['CCI5500', 'CCI5000', 'CCI4500'].map((grade, index) => ({
              grade,
              returned: index < hits ? 1 : 0,
              latestQuoteDate: index < hits ? '2026-11-03' : null,
              targetHits: index < hits ? 1 : 0,
              error: outcome === 'failed' ? '网络请求失败' : null,
            })),
          },
        },
      }),
    )
    await page.goto('/commodities?instrument=cci')
    await expect(page.getByText(stage, { exact: false })).toBeVisible()
    const status = page.getByRole('status', { name: 'CCI当日更新状态' })
    await expect(status).toContainText(`命中 ${hits}/3 档`)
    await expect(status).toContainText('目标日期：2026-11-03')
    await expect(page.getByRole('row')).toHaveCount(587)
    await expect(page.getByRole('button', { name: '更新今日' })).toBeEnabled()
    await expect(page.getByText(/补采/)).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
