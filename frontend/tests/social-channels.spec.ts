import { expect, test, type Page } from '@playwright/test'

async function mockMonitor(page: Page, configured = false) {
  const status = {
    enabled: true,
    telegram_enabled: true,
    feishu_enabled: false,
    token_configured: true,
    chat_id_configured: true,
    feishu_webhook_configured: configured,
    feishu_secret_configured: configured,
    interval_min: 600,
    interval_max: 960,
    authors: [
      {
        id: 1,
        uid: '123',
        name: '测试博主',
        platform: 'weibo',
        enabled: 1,
        last_check: null,
        last_error: null,
      },
    ],
    cubes: [],
    counts: { sent: 1, pending: 0 },
    last_check: null,
    next_check: null,
    last_error: null,
    browser_login_available: false,
    weibo_cookie_configured: true,
    xueqiu_cookie_configured: true,
  }
  const changes: Record<string, unknown>[] = []
  const tests: string[] = []
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.includes('/social-monitor'))
      return route.fulfill({ status: 503, json: { detail: '仅模拟博主监控接口' } })
    if (path.endsWith('/posts'))
      return route.fulfill({
        json: {
          total: 1,
          page: 1,
          items: [
            {
              id: 1,
              platform: 'weibo',
              author_name: '测试博主',
              status: 'muted',
              last_error: null,
              discovered_at: '2026-10-28T12:00:00+08:00',
              sent_at: null,
              payload: {
                name: '测试博主',
                text: '仅采集的公开动态',
                time: '2026-10-28T12:00:00+08:00',
                url: 'https://weibo.com/123',
              },
            },
          ],
        },
      })
    if (path.endsWith('/config')) {
      const body = route.request().postDataJSON()
      changes.push(body)
      for (const key of ['telegram_enabled', 'feishu_enabled', 'enabled'] as const)
        if (typeof body[key] === 'boolean') status[key] = body[key]
      if (body.feishu_webhook) status.feishu_webhook_configured = true
      if (body.feishu_secret) status.feishu_secret_configured = true
    }
    if (path.endsWith('/telegram-test') || path.endsWith('/feishu-test')) {
      tests.push(path)
      return route.fulfill({
        json: {
          message: path.endsWith('/feishu-test')
            ? '测试消息已发送，请检查飞书群'
            : '测试消息已发送，请检查 Telegram',
        },
      })
    }
    return route.fulfill({ json: status })
  })
  return { status, changes, tests }
}

test('notification channels can both run or independently stop while collection stays enabled', async ({
  page,
}) => {
  const state = await mockMonitor(page)
  await page.goto('/social-monitor')
  await expect(page.getByText('通知已关闭', { exact: true })).toBeVisible()
  await page.getByRole('tab', { name: '通知设置' }).click()
  const telegram = page.getByRole('switch', { name: '启用Telegram通知' })
  const feishu = page.getByRole('switch', { name: '启用飞书通知' })
  await expect(telegram).toBeChecked()
  await expect(feishu).not.toBeChecked()
  await expect(page.getByRole('button', { name: '发送飞书测试消息' })).toBeDisabled()
  await page
    .getByLabel('飞书 Webhook', { exact: true })
    .fill('https://open.feishu.cn/open-apis/bot/v2/hook/test-private-hook')
  await page.getByLabel('飞书签名密钥', { exact: true }).fill('test-private-signing-key')
  await page.getByRole('button', { name: '保存飞书配置' }).click()
  expect(state.changes[0]).toEqual({
    feishu_webhook: 'https://open.feishu.cn/open-apis/bot/v2/hook/test-private-hook',
    feishu_secret: 'test-private-signing-key',
  })
  await expect(page.getByRole('group', { name: '飞书 Webhook已配置' })).toContainText('***')
  await expect(page.getByRole('group', { name: '飞书签名密钥已配置' })).toContainText('***')
  expect(await page.content()).not.toContain('test-private-hook')
  expect(await page.content()).not.toContain('test-private-signing-key')
  await feishu.click()
  await expect(feishu).toBeChecked()
  await expect(telegram).toBeChecked()
  await page.getByRole('button', { name: '发送飞书测试消息' }).click()
  await expect(page.getByRole('status')).toContainText('请检查飞书群')
  await page.getByRole('button', { name: '发送测试消息', exact: true }).click()
  expect(state.tests).toEqual([
    '/api/v1/social-monitor/feishu-test',
    '/api/v1/social-monitor/telegram-test',
  ])
  await telegram.click()
  await expect(telegram).not.toBeChecked()
  await expect(feishu).toBeChecked()
  await expect(page.getByRole('button', { name: '发送测试消息', exact: true })).toBeDisabled()
  await feishu.click()
  await expect(feishu).not.toBeChecked()
  await expect(page.getByText('通知渠道均已关闭，监控运行时只采集、不推送。')).toBeVisible()
  await expect(page.getByText('后台监控中', { exact: true })).toBeVisible()
  expect(state.status.enabled).toBe(true)
  expect(state.changes.slice(1)).toEqual([
    { feishu_enabled: true },
    { telegram_enabled: false },
    { feishu_enabled: false },
  ])
  for (const change of state.changes) expect(change).not.toHaveProperty('enabled')
  await telegram.click()
  await expect(telegram).toBeChecked()
  await expect(feishu).not.toBeChecked()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('missing configuration prevents channel activation and blank edits preserve saved secrets', async ({
  page,
}) => {
  const state = await mockMonitor(page)
  await page.goto('/social-monitor')
  await page.getByRole('tab', { name: '通知设置' }).click()
  await page.getByRole('switch', { name: '启用飞书通知' }).click()
  await expect(page.getByRole('alert')).toContainText('请先保存飞书的飞书 Webhook和飞书签名密钥')
  expect(state.changes).toEqual([])
  await page
    .getByLabel('飞书 Webhook', { exact: true })
    .fill('https://open.feishu.cn/open-apis/bot/v2/hook/replaceable')
  await page.getByLabel('飞书签名密钥', { exact: true }).fill('saved-private-secret')
  await page.getByRole('button', { name: '保存飞书配置' }).click()
  await page.getByRole('button', { name: '修改飞书签名密钥' }).click()
  await expect(page.getByLabel('飞书签名密钥', { exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: '保存飞书签名密钥' })).toBeDisabled()
  await page.getByLabel('飞书签名密钥', { exact: true }).fill('  ')
  await expect(page.getByRole('button', { name: '保存飞书签名密钥' })).toBeDisabled()
  await page.getByRole('button', { name: '取消修改飞书签名密钥' }).click()
  expect(state.changes).toHaveLength(1)
  await page.getByRole('switch', { name: '启用飞书通知' }).click()
  await expect(page.getByRole('switch', { name: '启用飞书通知' })).toBeChecked()
  expect(state.changes.at(-1)).toEqual({ feishu_enabled: true })
  expect(await page.content()).not.toContain('saved-private-secret')
  await page.reload()
  await page.getByRole('tab', { name: '通知设置' }).click()
  await expect(page.getByRole('group', { name: '飞书签名密钥已配置' })).toBeVisible()
  expect(await page.content()).not.toContain('saved-private-secret')
})

test('disabled notifications still allow starting monitoring and channel errors are visible', async ({
  page,
}) => {
  const state = await mockMonitor(page, true)
  state.status.telegram_enabled = state.status.feishu_enabled = state.status.enabled = false
  await page.goto('/social-monitor')
  await page.getByRole('button', { name: '启动监控', exact: true }).click()
  await expect(page.getByText('后台监控中', { exact: true })).toBeVisible()
  expect(state.changes).toEqual([{ enabled: true }])
  await page.getByRole('tab', { name: '通知设置' }).click()
  await page.route('**/api/v1/social-monitor/config', (route) =>
    route.fulfill({ status: 400, json: { detail: '飞书配置不可用，请重新保存。' } }),
  )
  await page.getByRole('switch', { name: '启用飞书通知' }).click()
  await expect(page.getByRole('alert')).toContainText('飞书配置不可用')
  await expect(page.getByRole('switch', { name: '启用飞书通知' })).not.toBeChecked()
  await expect(page.getByText('后台监控中', { exact: true })).toBeVisible()
})
