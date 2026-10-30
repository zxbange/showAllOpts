import { expect, test } from '@playwright/test'

test('pair, enter database, select strategies and start directly; refresh, pause, cancel and logout', async ({
  page,
}) => {
  let authenticated = false
  let saved: Record<string, unknown> | null = null
  let job: Record<string, unknown> | null = null
  let saves = 0
  let starts = 0
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], trade_date: null, dates: [], counts: {} } }),
  )
  await page.route('**/api/v1/short-term/validation/**', async (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({
        json: {
          mode: 'pair',
          authenticated,
          https_required: false,
          ...(authenticated ? { csrf_token: 'page-csrf' } : {}),
        },
      })
    expect(r.request().headers()['x-validation-client']).toBe('browser')
    expect(r.request().headers().authorization).toBeUndefined()
    if (path.endsWith('/pair')) {
      expect(r.request().postDataJSON()).toEqual({
        code: 'one-time-console-code',
        http_confirmed: true,
      })
      authenticated = true
      return r.fulfill({ json: { csrf_token: 'page-csrf', expires_at: Date.now() / 1000 + 28800 } })
    }
    if (!authenticated)
      return r.fulfill({ status: 401, json: { detail: '会话已失效，请重新配对' } })
    if (r.request().method() === 'POST')
      expect(r.request().headers()['x-validation-csrf']).toBe('page-csrf')
    if (path.endsWith('/logout')) {
      authenticated = false
      return r.fulfill({ json: { ok: true } })
    }
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
      expect(body.methods).toEqual(['bukou', 'xuli'])
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
  await expect(page.getByText(/backend\/validation_pair.py --origin/)).toBeVisible()
  await expect(page.getByLabel('一次性配对码')).toBeDisabled()
  await page.getByLabel('我确认在HTTP上由本人操作并接受明文传输风险').check()
  await page.getByLabel('一次性配对码').fill('one-time-console-code')
  await page.getByRole('button', { name: '配对并进入' }).click()
  await expect(page.getByText(/HTTP兼容模式已开启/)).toHaveCount(0)
  for (const [label, value] of [
    ['数据库地址', 'db.example'],
    ['数据库名', 'research'],
    ['数据库用户名', 'readonly'],
    ['数据库密码', 'only-page-memory-password'],
  ])
    await page.getByLabel(label, { exact: true }).fill(value)
  await page.getByRole('checkbox', { name: '红悬停', exact: true }).uncheck()
  await page.getByRole('checkbox', { name: '超跌后止跌反弹', exact: true }).uncheck()
  await page.getByLabel('验证开始日', { exact: true }).fill('2024-02-01')
  await page.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect(page.getByRole('heading', { name: '补票龙 · 蓄力龙 — 运行中' })).toBeVisible()
  expect(saves).toBe(1)
  expect(starts).toBe(1)
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
  await page.reload()
  await expect(page.getByRole('heading', { name: '补票龙 · 蓄力龙 — 运行中' })).toBeVisible()
  await page.getByRole('button', { name: '暂停任务', exact: true }).click()
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await page.getByRole('button', { name: '取消任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '重试', exact: true })).toHaveCount(0)
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toMatch(/one-time-console-code|page-csrf|only-page-memory-password/)
  await page.getByRole('button', { name: '锁定验证管理', exact: true }).click()
  await expect(page.getByLabel('一次性配对码')).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('一次性配对码')).toBeVisible()
})

test('expired cookie clears form and returns to pairing without leaking inputs', async ({
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
  await expect(page.getByLabel('一次性配对码')).toBeVisible()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveCount(0)
  await expect(page.getByText(/会话已过期/)).toBeVisible()
})
