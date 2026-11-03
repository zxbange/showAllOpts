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
  await page.getByRole('button', { name: '更新 / 补采' }).click()
  expect(calls).toBe(1)
  await expect(page.getByRole('button', { name: '更新 / 补采' })).toBeEnabled()
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
  await page.getByRole('button', { name: '更新 / 补采' }).click()
  await expect(page.getByRole('button', { name: '更新中…' })).toBeDisabled()
  expect(posts).toBe(1)
  release()
  await expect(page.getByRole('alert')).toContainText('CCI更新未启动')
  await expect(page.getByRole('row')).toHaveCount(587)
})
