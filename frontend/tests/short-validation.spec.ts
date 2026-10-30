import { expect, test, type Page } from '@playwright/test'
async function unlock(page: Page) {
  await page.getByLabel('验证管理密钥').fill('test-admin-key')
  await page.getByRole('button', { name: '解锁验证管理' }).click()
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/short-term/validation/access', (r) =>
    r.fulfill({
      json: {
        admin_configured: true,
        encryption_configured: true,
        targets_configured: true,
        https_required: false,
      },
    }),
  )
  await page.route('**/api/v1/short-term/validation/sources', (r) =>
    r.fulfill({ json: { sources: [] } }),
  )
})

test('select strategies, resume failure, refresh durable task and show unavailable rates', async ({
  page,
}) => {
  await page.addInitScript(() => Object.defineProperty(crypto, 'randomUUID', { value: undefined }))
  const jobs: Record<string, unknown>[] = []
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({ json: { trade_date: null, dates: [], counts: {}, items: [] } }),
  )
  await page.route('**/api/v1/short-term/validation/jobs**', async (r) => {
    if (r.request().method() === 'GET') return r.fulfill({ json: { jobs } })
    if (r.request().url().endsWith('/retry')) {
      jobs[0] = {
        ...jobs[0],
        status: 'completed',
        stage: '完成',
        error: null,
        done: 1,
        summary: [{ 策略: '补票龙', 后续交易日: 1, 有效样本: 0, '胜率(%)': null }],
        windows: [],
      }
      return r.fulfill({ json: jobs[0] })
    }
    const body = r.request().postDataJSON()
    expect(body.methods).toEqual(['bukou', 'oversold'])
    expect(body.start).toBe('2024-01-01')
    expect(body.end).toBeNull()
    jobs[0] = {
      ...body,
      id: 'job1',
      status: 'failed',
      stage: '计算 000001.XSHE',
      captured: 1,
      total: 1,
      done: 0,
      updated_at: '2026-10-30T00:00:00Z',
      error: '测试查询超时',
      version: 'four-close-v1',
      summary: [],
      windows: [],
    }
    return r.fulfill({ json: jobs[0] })
  })
  await page.goto('/short-term/validation')
  await unlock(page)
  const panel = page.getByRole('region', { name: '短线胜率验证' })
  await expect(panel.getByRole('checkbox')).toHaveCount(4)
  await panel.getByRole('checkbox', { name: '红悬停', exact: true }).uncheck()
  await panel.getByRole('checkbox', { name: '蓄力龙', exact: true }).uncheck()
  await panel.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('测试查询超时')
  await page.reload()
  await unlock(page)
  await expect(panel.getByRole('button', { name: '从断点重试' })).toBeVisible()
  await panel.getByRole('button', { name: '从断点重试' }).click()
  await expect(panel.getByText('部分策略/期限有效样本不足30', { exact: false })).toBeVisible()
  await expect(panel.getByRole('cell', { name: '—', exact: true })).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty(
    'scrollWidth',
    await page.evaluate(() => document.body.clientWidth),
  )
})

test('empty selection blocks submission', async ({ page }) => {
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({
      json: {
        jobs: [],
        enabled: false,
        modules: [],
        trade_date: null,
        dates: [],
        counts: {},
        https_required: false,
      },
    }),
  )
  await page.goto('/short-term/validation')
  await unlock(page)
  const panel = page.getByRole('region', { name: '短线胜率验证' })
  await expect(panel.getByRole('checkbox')).toHaveCount(4)
  for (const box of await panel.getByRole('checkbox').all()) await box.uncheck()
  await expect(panel.getByRole('button', { name: '开始验证', exact: true })).toBeDisabled()
})

test('validation has its own peer page and supports reload, deep link and back', async ({
  page,
}) => {
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({ json: { trade_date: null, dates: [], counts: {}, stocks: [], total: 0 } }),
  )
  await page.goto('/short-term?method=xuli')
  await expect(page.getByRole('link', { name: '蓄力龙', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.getByLabel('验证管理密钥')).toHaveCount(0)
  await page.getByRole('link', { name: '胜率验证', exact: true }).click()
  await expect(page).toHaveURL(/\/short-term\/validation$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('短线胜率验证')
  await expect(page.getByRole('link', { name: '胜率验证', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.reload()
  await expect(page.getByLabel('验证管理密钥')).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/method=xuli$/)
  await expect(page.getByRole('link', { name: '蓄力龙', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
})
