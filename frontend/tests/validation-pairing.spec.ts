import { expect, test } from './fixtures'

test('account session, enter database, select strategies and start directly; refresh, pause and cancel', async ({
  page,
}) => {
  let saved: Record<string, unknown> | null = null
  let job: Record<string, unknown> | null = null
  let saves = 0
  let starts = 0
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], trade_date: null, dates: [], counts: {} } }),
  )
  await page.route('**/api/v1/short-term/validation/**', async (r) => {
    const path = new URL(r.request().url()).pathname
    expect(r.request().headers().authorization).toBeUndefined()
    if (r.request().method() === 'POST')
      expect(r.request().headers()['x-csrf-token']).toBe('test-csrf')
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: saved ? [saved] : [] } })
    if (path.endsWith('/sources/save')) {
      saves++
      const body = r.request().postDataJSON()
      expect(body.password).toBe('only-page-memory-password')
      const { password: _, ...publicData } = body
      saved = { ...publicData, id: 'a'.repeat(32), revision: 1, password_set: true }
      return r.fulfill({ json: saved })
    }
    if (path.endsWith('/jobs') && r.request().method() === 'POST') {
      starts++
      const body = r.request().postDataJSON()
      expect(body.source).toEqual({ id: 'a'.repeat(32), revision: 1 })
      expect(body.methods).toEqual(['bukou', 'oversold'])
      expect(body.start).toBe('2024-02-01')
      job = {
        id: 'job1',
        ...body,
        version: 'four-close-v1',
        status: 'running',
        stage: '计算中',
        done: 0,
        captured: 1,
        total: 1,
        summary: [],
        windows: [],
        updated_at: '2026-10-30T00:00:00Z',
      }
      return r.fulfill({ json: job })
    }
    if (path.endsWith('/pause')) job = { ...job, status: 'paused' }
    if (path.endsWith('/retry')) job = { ...job, status: 'running' }
    if (path.endsWith('/cancel')) job = { ...job, status: 'cancelled' }
    if (r.request().method() === 'POST') return r.fulfill({ json: job })
    return r.fulfill({ json: { jobs: job ? [job] : [] } })
  })
  await page.goto('/short-term/validation')
  await expect(page.getByLabel('一次性配对码')).toHaveCount(0)
  for (const [label, value] of [
    ['数据库地址', 'db.example'],
    ['数据库名', 'research'],
    ['数据库用户名', 'readonly'],
    ['数据库密码', 'only-page-memory-password'],
  ])
    await page.getByLabel(label, { exact: true }).fill(value)
  await page.getByLabel('验证开始日', { exact: true }).fill('2024-02-01')
  await page.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '补票龙 · 超跌后止跌反弹 — 运行中' }),
  ).toBeVisible()
  expect(saves).toBe(1)
  expect(starts).toBe(1)
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
  await page.reload()
  await expect(
    page.getByRole('heading', { name: '补票龙 · 超跌后止跌反弹 — 运行中' }),
  ).toBeVisible()
  await page.getByRole('button', { name: '暂停任务', exact: true }).click()
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await page.getByRole('button', { name: '取消任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '重试', exact: true })).toHaveCount(0)
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toMatch(/one-time-console-code|page-csrf|only-page-memory-password/)
  await expect(page.getByRole('button', { name: '锁定验证管理', exact: true })).toHaveCount(0)
  await page.reload()
  await expect(
    page.getByRole('heading', { name: '补票龙 · 超跌后止跌反弹 — 已取消' }),
  ).toBeVisible()
})

test('expired cookie clears form and returns to site login without leaking inputs', async ({
  page,
}) => {
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { enabled: false, modules: [] } }))
  await page.route('**/api/v1/short-term/validation/access', (r) =>
    r.fulfill({
      json: { mode: 'pair', authenticated: true, csrf_token: 'csrf', https_required: false },
    }),
  )
  await page.route('**/api/v1/short-term/validation/sources', (r) =>
    r.fulfill({ json: { sources: [] } }),
  )
  await page.route('**/api/v1/short-term/validation/jobs', (r) =>
    r.fulfill({ status: 401, json: { detail: '会话已失效，请重新配对' } }),
  )
  await page.goto('/short-term/validation')
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveCount(0)
  await expect(page.getByText(/登录已过期/)).toBeVisible()
})
