import { expect, test, type Page } from './fixtures'
async function unlock(page: Page) {
  await expect(page.getByRole('region', { name: '短线胜率验证' })).toBeVisible()
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
      error:
        'OperationalError：读取源库行情失败；源数据库连接在查询期间中断，可能涉及超时、网络或服务状态，请检查后重试（错误码：MYSQL_2013；任务：0123456789abcdef0123456789abcdef）。已提交断点保留；修复后点击重试。',
      version: 'four-close-v1',
      summary: [],
      windows: [],
    }
    return r.fulfill({ json: jobs[0] })
  })
  await page.route('**/api/v1/short-term/validation/sources/save', (r) =>
    r.fulfill({ json: { id: 'a'.repeat(32), revision: 1, password_set: true } }),
  )
  await page.goto('/short-term/validation')
  await unlock(page)
  for (const [label, value] of [
    ['数据库地址', 'db.example'],
    ['数据库名', 'research'],
    ['数据库用户名', 'readonly'],
    ['数据库密码', 'test-private-password'],
  ]) {
    await page.getByLabel(label, { exact: true }).fill(value)
  }
  const panel = page.getByRole('region', { name: '短线胜率验证' })
  await expect(panel.getByRole('checkbox')).toHaveCount(2)
  await panel.getByRole('checkbox', { name: '超跌后止跌反弹', exact: true }).uncheck()
  await panel.getByRole('checkbox', { name: '超跌后止跌反弹', exact: true }).check()
  await panel.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('MYSQL_2013')
  await expect(panel.getByRole('alert')).toContainText('断点保留')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.reload()
  await unlock(page)
  await expect(panel.getByRole('button', { name: '重试' })).toBeVisible()
  await panel.getByRole('button', { name: '重试' }).click()
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
  await expect(panel.getByRole('checkbox')).toHaveCount(2)
  for (const box of await panel.getByRole('checkbox').all()) await box.uncheck()
  await expect(panel.getByRole('button', { name: '开始验证', exact: true })).toBeDisabled()
})

test('validation has its own peer page and supports reload, deep link and back', async ({
  page,
}) => {
  await page.route('**/api/v1/short-term/latest?*', (r) =>
    r.fulfill({ json: { trade_date: null, dates: [], counts: {}, stocks: [], total: 0 } }),
  )
  await page.goto('/short-term?method=bukou')
  await expect(page.getByRole('link', { name: '补票龙', exact: true })).toHaveAttribute(
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
  await expect(page.getByRole('region', { name: '短线胜率验证' })).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/method=bukou$/)
  await expect(page.getByRole('link', { name: '补票龙', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
})
