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
    tushare: { token_set: true },
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
    market: { enabled: true, times: ['00:01'], telegram_notify: false, feishu_notify: false },
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
    earnings_selection: {
      enabled: true,
      times: ['23:00'],
      telegram_notify: false,
      feishu_notify: false,
    },
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
  await expect(page.getByLabel('Tushare Token', { exact: true })).toHaveValue('')
  await expect(page.getByText(/TLS|读写账号必须分开/)).toHaveCount(0)
  await page.getByRole('button', { name: '测试数据库连接' }).click()
  await expect(page.getByRole('status')).toContainText('未验证写入权限')
  expect(tests).toBe(1)
  expect(saves).toHaveLength(0)
  for (const label of [
    '密码',
    'AShareHub Token',
    'Tushare Token',
    'Telegram 机器人密钥',
    'Telegram 接收会话编号',
  ]) {
    await expect(page.getByLabel(label, { exact: true })).toHaveAttribute('placeholder', '********')
    await expect(page.getByLabel(label, { exact: true })).toHaveValue('')
  }
  await page.getByLabel('日志级别', { exact: true }).selectOption('WARNING')
  await page.getByLabel('保留天数', { exact: true }).fill('45')
  await page.getByLabel('读取超时（秒）', { exact: true }).fill('900')
  await page.getByLabel('尝试次数（含首次）', { exact: true }).fill('4')
  await page.getByLabel('AShareHub请求超时（秒）', { exact: true }).fill('120')
  await expect(page.getByText('尚未检测可用性', { exact: false }).first()).toBeVisible()
  const market = page.locator('.system-task').filter({ hasText: '市场热度 / 抱团拥挤度' })
  await market.getByLabel('每日时间').fill('00:05')
  await market.getByLabel('Telegram 通知').click()
  await expect(page.getByRole('status')).toHaveText('开关已保存并生效')
  expect(saves[0].schedules.market.times).toEqual(['00:01'])
  expect(saves[0].runtime.logging.level).toBe('INFO')
  expect(saves[0].database.password).toBeNull()
  expect(saves[0]).not.toHaveProperty('integrations')
  expect(saves[0].schedules.market.telegram_notify).toBe(true)
  await expect(market.getByLabel('每日时间')).toHaveValue('00:05')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect(page.getByRole('status')).toHaveText('系统配置已保存')
  expect(saves[1].database.password).toBeNull()
  expect(saves[1].runtime.logging).toEqual({ level: 'WARNING', retention_days: 45 })
  expect(saves[1].runtime.database_sync).toEqual({ read_timeout_seconds: 900, attempts: 4 })
  expect(saves[1].runtime.sources.asharehub.request_timeout_seconds).toBe(120)
  expect(saves[1]).not.toHaveProperty('data_sources')
  expect(saves[1].integrations).toEqual({
    asharehub: { token: null },
    tushare: { token: null },
    telegram: { token: null, chat_id: null },
  })
  expect(saves[1].schedules.market.times).toEqual(['00:05'])
  await page.getByLabel('AShareHub Token', { exact: true }).fill('test-asharehub-token')
  await page.getByLabel('Tushare Token', { exact: true }).fill('test-tushare-token')
  await expect(page.getByLabel('Tushare Token', { exact: true })).toHaveAttribute(
    'type',
    'password',
  )
  await page.getByLabel('Telegram 机器人密钥', { exact: true }).fill('test-telegram-token')
  await page.getByLabel('Telegram 接收会话编号', { exact: true }).fill('test-chat-id')
  await page.getByLabel('主机', { exact: true }).fill('new.example')
  await page.getByLabel('密码', { exact: true }).fill('not-persisted-in-browser')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect.poll(() => saves.length).toBe(3)
  expect(saves[2].integrations).toEqual({
    asharehub: { token: 'test-asharehub-token' },
    tushare: { token: 'test-tushare-token' },
    telegram: { token: 'test-telegram-token', chat_id: 'test-chat-id' },
  })
  await expect(page.getByLabel('AShareHub Token', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Tushare Token', { exact: true })).toHaveValue('')
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toContain('not-persisted-in-browser')
  await page.reload()
  await expect(market.getByLabel('每日时间')).toHaveValue('00:05')
  await page.getByRole('button', { name: '清除Tushare Token', exact: true }).click()
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect.poll(() => saves.length).toBe(4)
  expect(saves[3].integrations.tushare.token).toBe('')
  await page.getByRole('button', { name: '清除密码', exact: true }).click()
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect.poll(() => saves.length).toBe(5)
  expect(saves[4].database.password).toBe('')
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
  await page.getByLabel('Tushare Token', { exact: true }).fill('retain-tushare-draft')
  await page.getByLabel('主机', { exact: true }).fill('draft.example')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect(page.getByRole('alert')).toContainText('配置版本已更新')
  await expect(page.getByRole('dialog', { name: '配置操作失败' })).toBeVisible()
  await expect(page.getByRole('dialog')).toContainText('配置版本已更新')
  await page.getByRole('button', { name: '知道了' }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.getByLabel('AShareHub Token', { exact: true })).toHaveValue('retain-draft')
  await expect(page.getByLabel('Tushare Token', { exact: true })).toHaveValue(
    'retain-tushare-draft',
  )
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
  await expect(page.getByLabel('Tushare Token', { exact: true })).toHaveValue('')
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

test('schedule columns, explicit switches and 24-hour fields remain independent', async ({
  page,
}, testInfo) => {
  let config = initial()
  let saves = 0
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { modules: [] } }))
  await page.route('**/api/v1/system-configuration', (r) => {
    if (r.request().method() === 'PUT') {
      saves++
      config = { ...config, schedules: r.request().postDataJSON().schedules }
    }
    return r.fulfill({ json: config })
  })
  await page.goto('/system-configuration')
  const market = page.getByRole('row', { name: '市场热度 / 抱团拥挤度', exact: true })
  const auto = market.getByRole('switch', { name: '自动运行', exact: true })
  const notify = market.getByRole('switch', { name: 'Telegram 通知', exact: true })
  const feishu = market.getByRole('switch', { name: '飞书通知', exact: true })
  await expect(auto).toBeChecked()
  await expect(notify).not.toBeChecked()
  await expect(feishu).not.toBeChecked()
  await feishu.click()
  await expect(feishu).toBeChecked()
  await expect(notify).not.toBeChecked()
  await auto.click()
  await expect(auto).not.toBeChecked()
  await notify.click()
  await expect(auto).not.toBeChecked()
  await expect(notify).toBeChecked()
  expect(saves).toBe(3)
  await auto.focus()
  await page.keyboard.press('Space')
  await expect(auto).toBeChecked()
  const time = market.getByLabel('每日时间')
  await expect(time).toHaveAttribute('type', 'text')
  await time.fill('23:59')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  await expect.poll(() => saves).toBe(5)
  expect(config.schedules.market.times).toEqual(['23:59'])
  expect(config.schedules.market.telegram_notify).toBe(true)
  expect(config.schedules.market.feishu_notify).toBe(true)
  await time.fill('24:01')
  await page.getByRole('button', { name: '保存系统配置' }).click()
  expect(saves).toBe(5)
  await time.fill('1230')
  await expect(time).toHaveValue('12:30')
  await page.getByRole('button', { name: '重新读取配置' }).click()
  await expect(time).toHaveValue('23:59')
  const earnings = page.getByRole('row', { name: '业绩选股（高股息 / 奶牛股）', exact: true })
  await expect(earnings).toContainText('交易日')
  await expect(earnings.getByLabel('每日时间')).toHaveValue('23:00')
  await expect(page.getByText('每月最后一天', { exact: true })).toHaveCount(0)
  await expect(page.locator('form form')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `/tmp/settings-${testInfo.project.name}.png`, fullPage: true })
})

test('failed switch save keeps the last persisted value and all draft inputs', async ({ page }) => {
  const config = initial()
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { modules: [] } }))
  await page.route('**/api/v1/system-configuration', (r) => {
    if (r.request().method() === 'PUT') {
      const body = r.request().postDataJSON()
      expect(body.database.host).toBe('db.example')
      expect(body.database.password).toBeNull()
      expect(body.schedules.market.times).toEqual(['00:01'])
      expect(body.schedules.market.feishu_notify).toBe(true)
      return r.fulfill({ status: 409, json: { detail: '配置已更新，请刷新后重试' } })
    }
    return r.fulfill({ json: config })
  })
  await page.goto('/system-configuration')
  const market = page.getByRole('row', { name: '市场热度 / 抱团拥挤度', exact: true })
  await market.getByLabel('每日时间').fill('22:00')
  await page.getByLabel('主机', { exact: true }).fill('draft.example')
  await page.getByLabel('密码', { exact: true }).fill('draft-password')
  const feishu = market.getByRole('switch', { name: '飞书通知' })
  await feishu.click()
  await expect(page.getByRole('alert').first()).toContainText('配置已更新')
  await expect(feishu).not.toBeChecked()
  await expect(market.getByLabel('每日时间')).toHaveValue('22:00')
  await expect(page.getByLabel('主机', { exact: true })).toHaveValue('draft.example')
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('draft-password')
})
