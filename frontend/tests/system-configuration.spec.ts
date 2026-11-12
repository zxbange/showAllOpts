import { test, expect, adminSession } from './fixtures'
const initial = () => ({
  revision: 1,
  database_revision: 1,
  timezone: 'Asia/Shanghai',
  runtime: {
    logging: { level: 'INFO', retention_days: 30 },
    database_sync: { read_timeout_seconds: 600, attempts: 3 },
    sources: {
      asharehub: { request_timeout_seconds: 60 },
      eastmoney: { request_timeout_seconds: 60 },
    },
  },
  data_sources: [
    {
      id: 'asharehub',
      credential_required: true,
      credential_set: true,
      status: 'configured',
      health: 'not_checked',
      read_only: true,
    },
  ],
  integrations: {
    asharehub: { token_set: true },
    telegram: { token_set: true, chat_id_set: true },
  },
  database: {
    host: 'db.example',
    port: 3306,
    database: 'stocks',
    username: 'collector',
    password_set: true,
  },
  schedules: {
    cache: { enabled: true, times: ['07:00'] },
    market: { enabled: true, times: ['00:01'], telegram_notify: false },
    etf: { enabled: true, times: ['09:00'] },
    commodities: { enabled: true, times: ['11:00', '18:00'] },
    holdings: {
      enabled: false,
      start: '09:25',
      end: '15:10',
      interval_seconds: 15,
      table: 'positions',
    },
    social: {
      enabled: false,
      start: '07:00',
      end: '24:00',
      min_interval_minutes: 10,
      max_interval_minutes: 16,
    },
    cash_cow: { enabled: true, times: ['21:00'], frequency: 'month_end' },
  },
})
test('admin saves shared database, integrations and times without secret echo', async ({
  page,
}) => {
  let config = initial()
  const saves: any[] = []
  let tests = 0
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { modules: [] } }))
  await page.route('**/api/v1/system-configuration**', async (r) => {
    if (r.request().url().endsWith('/test')) {
      tests++
      return r.fulfill({ json: { ok: true, message: '连接成功；未验证写入权限' } })
    }
    if (r.request().method() === 'PUT') {
      const b = r.request().postDataJSON()
      saves.push(b)
      expect(r.request().headers()['x-csrf-token']).toBe('test-csrf')
      const { password, ...database } = b.database
      config = {
        ...config,
        ...b,
        integrations: initial().integrations,
        database: { ...database, password_set: true },
        revision: config.revision + 1,
      }
    }
    return r.fulfill({ json: config })
  })
  await page.goto('/system-configuration')
  await expect(page.getByRole('heading', { name: '统一数据库' })).toBeVisible()
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('')
  await expect(page.getByText(/TLS|读写账号必须分开/)).toHaveCount(0)
  await page.getByRole('button', { name: '测试数据库连接' }).click()
  await expect(page.getByRole('status')).toContainText('未验证写入权限')
  expect(tests).toBe(1)
  expect(saves).toHaveLength(0)
  await page.getByLabel('日志级别', { exact: true }).selectOption('WARNING')
  await page.getByLabel('保留天数', { exact: true }).fill('45')
  await page.getByLabel('读取超时（秒）', { exact: true }).fill('900')
  await page.getByLabel('尝试次数（含首次）', { exact: true }).fill('4')
  await page.getByLabel('AShareHub请求超时（秒）', { exact: true }).fill('120')
  await expect(page.getByText('尚未检测可用性', { exact: false }).first()).toBeVisible()
  const market = page.locator('.system-task').filter({ hasText: '市场热度 / 抱团拥挤度' })
  await market.getByLabel('每日时间').fill('00:05')
  await market.getByLabel('Telegram 通知').check()
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect(page.getByRole('status')).toHaveText('系统配置已保存')
  expect(saves[0].database.password).toBeNull()
  expect(saves[0].runtime.logging).toEqual({ level: 'WARNING', retention_days: 45 })
  expect(saves[0].runtime.database_sync).toEqual({ read_timeout_seconds: 900, attempts: 4 })
  expect(saves[0].runtime.sources.asharehub.request_timeout_seconds).toBe(120)
  expect(saves[0]).not.toHaveProperty('data_sources')
  expect(saves[0].integrations).toEqual({
    asharehub: { token: null },
    telegram: { token: null, chat_id: null },
  })
  expect(saves[0].schedules.market.times).toEqual(['00:05'])
  await page.getByLabel('AShareHub Token', { exact: true }).fill('test-asharehub-token')
  await page.getByLabel('Telegram 机器人密钥', { exact: true }).fill('test-telegram-token')
  await page.getByLabel('Telegram 接收会话编号', { exact: true }).fill('test-chat-id')
  await page.getByLabel('主机', { exact: true }).fill('new.example')
  await page.getByLabel('密码', { exact: true }).fill('not-persisted-in-browser')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect.poll(() => saves.length).toBe(2)
  expect(saves[1].integrations).toEqual({
    asharehub: { token: 'test-asharehub-token' },
    telegram: { token: 'test-telegram-token', chat_id: 'test-chat-id' },
  })
  await expect(page.getByLabel('AShareHub Token', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('')
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toContain('not-persisted-in-browser')
  await page.reload()
  await expect(market.getByLabel('每日时间')).toHaveValue('00:05')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
test('save conflict retains all draft secrets and does not claim success', async ({ page }) => {
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { modules: [] } }))
  await page.route('**/api/v1/system-configuration', (r) =>
    r.request().method() === 'PUT'
      ? r.fulfill({ status: 409, json: { detail: '配置版本已更新' } })
      : r.fulfill({ json: initial() }),
  )
  await page.goto('/system-configuration')
  await page.getByLabel('AShareHub Token', { exact: true }).fill('retain-draft')
  await page.getByLabel('主机', { exact: true }).fill('draft.example')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect(page.getByRole('alert')).toContainText('配置版本已更新')
  await expect(page.getByLabel('AShareHub Token', { exact: true })).toHaveValue('retain-draft')
  await expect(page.getByRole('status')).toHaveCount(0)
})
test('viewer cannot request configuration or see navigation', async ({ page }) => {
  let calls = 0
  await page.route('**/api/auth/**', (r) =>
    r.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'user' } } }),
  )
  await page.route('**/api/v1/system-configuration**', (r) => {
    calls++
    return r.fulfill({ json: initial() })
  })
  await page.goto('/system-configuration')
  await expect(page.getByText('仅管理员可访问')).toBeVisible()
  await expect(page.getByRole('link', { name: '系统配置' })).toHaveCount(0)
  expect(calls).toBe(0)
})
test('reload discards drafts and invalid time cannot submit', async ({ page }) => {
  let saves = 0
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { modules: [] } }))
  await page.route('**/api/v1/system-configuration', (r) => {
    if (r.request().method() === 'PUT') saves++
    return r.fulfill({ json: initial() })
  })
  await page.goto('/system-configuration')
  const social = page.locator('.system-task').filter({ hasText: '微博 / 雪球动态' })
  await social.getByLabel('最短周期（分钟）').fill('12')
  await page.getByLabel('密码', { exact: true }).fill('discard-me')
  await page.getByRole('button', { name: '重新读取配置' }).click()
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('')
  await expect(social.getByLabel('最短周期（分钟）')).toHaveValue('10')
  const etf = page.locator('.system-task').filter({ hasText: '国家队动态' })
  await expect(etf.getByLabel('每日时间')).toHaveCount(1)
  await etf.getByLabel('每日时间').fill('')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  expect(saves).toBe(0)
})
test('changed target rejection preserves entered target', async ({ page }) => {
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { modules: [] } }))
  await page.route('**/api/v1/system-configuration', (r) =>
    r.request().method() === 'PUT'
      ? r.fulfill({ status: 422, json: { detail: '更换目标请重新输入密码' } })
      : r.fulfill({ json: initial() }),
  )
  await page.goto('/system-configuration')
  await page.getByLabel('主机', { exact: true }).fill('remote.example')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect(page.getByRole('alert')).toContainText('重新输入密码')
  await expect(page.getByLabel('主机', { exact: true })).toHaveValue('remote.example')
})
