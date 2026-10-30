import { expect, test } from '@playwright/test'
test('admin config test-save keeps password in request only, version selection and cancellation', async ({
  page,
}) => {
  let saved: Record<string, unknown> | null = null
  let status = 'running'
  let saveAttempts = 0
  const job = () => ({
    id: 'job1',
    methods: ['bukou'],
    start: '2024-01-01',
    end: '2026-10-01',
    observed: '2026-10-05',
    version: 'four-close-v1',
    status,
    stage: status === 'cancelling' ? '正在取消，等待在途查询' : status,
    captured: 1,
    done: 0,
    total: 1,
    updated_at: '2026-10-30T00:00:00Z',
    summary: [],
    windows: [],
  })
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], trade_date: null, dates: [], counts: {} } }),
  )
  await page.route('**/api/v1/short-term/validation/**', (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({
        json: {
          admin_configured: true,
          encryption_configured: true,
          targets_configured: true,
          https_required: false,
        },
      })
    expect(r.request().headers().authorization).toBe('Bearer test-admin-key')
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: saved ? [saved] : [] } })
    if (path.endsWith('/sources/test') || path.endsWith('/sources/save')) {
      const body = r.request().postDataJSON()
      expect(body.password).toBe('temporary-db-password')
      if (path.endsWith('/test'))
        return r.fulfill({ json: { ok: true, message: '只读连接测试通过' } })
      if (++saveAttempts === 1)
        return r.fulfill({ status: 409, json: { detail: '模拟保存失败，请修正后重试' } })
      const { password: _, ...publicFields } = body
      saved = { ...publicFields, id: 'source1', revision: 1, password_set: true }
      return r.fulfill({ json: saved })
    }
    if (path.endsWith('/pause')) {
      status = 'paused'
      return r.fulfill({ json: job() })
    }
    if (path.endsWith('/retry')) {
      status = 'running'
      return r.fulfill({ json: job() })
    }
    if (path.endsWith('/cancel')) {
      status = 'cancelling'
      return r.fulfill({ json: job() })
    }
    if (r.request().method() === 'POST') {
      expect(r.request().postDataJSON().source).toEqual({ id: 'source1', revision: 1 })
      return r.fulfill({ json: job() })
    }
    return r.fulfill({ json: { jobs: [job()] } })
  })
  await page.goto('/short-term')
  await expect(page.getByText(/HTTP兼容模式已开启/)).toBeVisible()
  await page.getByLabel('验证管理密钥').fill('test-admin-key')
  await page.getByRole('button', { name: '解锁验证管理' }).click()
  await expect(page.getByText(/HTTP兼容模式已开启/)).toBeVisible()
  await page.getByText('新增或编辑数据库配置', { exact: true }).click()
  for (const [label, value] of [
    ['配置名称', '研究库'],
    ['数据库地址', 'db.example'],
    ['数据库名', 'research'],
    ['数据库用户名', 'readonly'],
    ['数据库密码', 'temporary-db-password'],
  ])
    await page.getByLabel(label, { exact: true }).fill(value)
  await page.getByRole('button', { name: '测试连接（只读）', exact: true }).click()
  await expect(page.getByText('只读连接测试通过')).toBeVisible()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('temporary-db-password')
  await page.getByRole('button', { name: '加密保存并使用' }).click()
  await expect(page.getByText('模拟保存失败，请修正后重试')).toBeVisible()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('temporary-db-password')
  await page.getByRole('button', { name: '加密保存并使用' }).click()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('验证数据源')).toHaveValue('source1:1')
  await page.getByRole('button', { name: '开始验证', exact: true }).click()
  await page.getByRole('button', { name: '暂停任务', exact: true }).click()
  await page.getByRole('button', { name: '从断点重试', exact: true }).click()
  await page.getByRole('button', { name: '取消任务（不可继续）', exact: true }).click()
  await expect(page.getByText('正在取消，等待在途查询', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '从断点重试', exact: true })).toHaveCount(0)
  status = 'cancelled'
  await expect(page.getByRole('heading', { name: '补票龙 — 已取消' })).toBeVisible()
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toMatch(/test-admin-key|temporary-db-password/)
  await page.getByRole('button', { name: '锁定验证管理', exact: true }).click()
  await expect(page.getByLabel('验证管理密钥')).toHaveValue('')
})

for (const httpsRequired of [true, undefined]) {
  test(`HTTP credential entry disabled unless explicitly allowed (${httpsRequired})`, async ({
    page,
  }) => {
    await page.addInitScript(() =>
      Object.defineProperty(window, 'isSecureContext', { value: false }),
    )
    await page.route('**/api/v1/**', (r) =>
      r.fulfill({ json: { enabled: false, modules: [], trade_date: null, dates: [], counts: {} } }),
    )
    await page.route('**/api/v1/short-term/validation/access', (r) =>
      r.fulfill({ json: { https_required: httpsRequired } }),
    )
    await page.goto('/short-term')
    await expect(page.getByLabel('验证管理密钥')).toBeDisabled()
    await expect(page.getByRole('button', { name: '解锁验证管理' })).toBeDisabled()
    await expect(page.getByText('请通过HTTPS访问后再输入管理密钥或数据库凭证。')).toBeVisible()
  })
}
