import { expect, test } from '@playwright/test'

test('initial loading failures can be retried without a login page', async ({ page }) => {
  let ready = false
  await page.route('**/api/v1/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.includes('/social-monitor') || !ready)
      return route.fulfill({ status: 503, json: { detail: '加载失败测试' } })
    expect(route.request().headers().authorization).toBeUndefined()
    return route.fulfill({
      json: path.endsWith('/posts')
        ? { total: 0, page: 1, items: [] }
        : {
            enabled: false,
            authors: [],
            counts: {},
            token_configured: false,
            chat_id_configured: false,
            weibo_cookie_configured: false,
            xueqiu_cookie_configured: false,
            last_check: null,
            next_check: null,
          },
    })
  })
  await page.goto('/social-monitor')
  await expect(page.getByRole('alert')).toContainText('加载失败测试')
  await expect(page.getByRole('button', { name: '登录监控台' })).toHaveCount(0)
  ready = true
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('heading', { name: '最新动态', exact: true })).toBeVisible()
})

test('open the monitor directly, configure users, notifications and inspect posts', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const status = {
    enabled: false,
    interval_min: 600,
    interval_max: 960,
    night_rest: false,
    platforms: {} as Record<
      string,
      {
        paused: boolean
        reason: string
        alert_sent_at: string | null
        http_status?: number
        paused_at?: string
      }
    >,
    last_check: null,
    next_check: null,
    last_error: null,
    token_configured: false,
    chat_id_configured: false,
    weibo_cookie_configured: false,
    xueqiu_cookie_configured: false,
    authors: [] as {
      id: number
      platform: string
      uid: string
      name: string
      enabled: number
      last_check: null
      last_error: null
    }[],
    counts: { sent: 1, pending: 0 },
  }
  let showFeed = false
  let postRequests = 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.includes('/social-monitor'))
      return route.fulfill({ status: 503, json: { detail: '模拟市场数据不可用' } })
    expect(request.headers().authorization).toBeUndefined()
    if (path.endsWith('/posts')) {
      postRequests++
      const keyword = new URL(request.url()).searchParams.get('keyword') || ''
      const matches = showFeed && (!keyword || '今天发布的测试正文'.includes(keyword))
      return route.fulfill({
        json: {
          total: matches ? 1 : 0,
          page: 1,
          items: matches
            ? [
                {
                  id: 1,
                  platform: 'weibo',
                  author_name: '测试博主',
                  status: 'sent',
                  last_error: null,
                  discovered_at: '2026-10-25T12:00:01+08:00',
                  sent_at: '2026-10-25T12:00:02+08:00',
                  payload: {
                    name: '测试博主',
                    time: '2026-10-25T12:00:00+08:00',
                    text: '今天发布的测试正文\n<script>不会执行</script>',
                    url: 'https://m.weibo.cn/detail/123',
                  },
                },
              ]
            : [],
        },
      })
    }
    if (path.endsWith('/config')) {
      const body = request.postDataJSON()
      if (body.token) status.token_configured = true
      if (body.chat_id) status.chat_id_configured = true
      if (typeof body.enabled === 'boolean') status.enabled = body.enabled
    }
    if (path.endsWith('/platforms/weibo/resume')) status.platforms.weibo.paused = false
    if (path.endsWith('/authors/1') && request.method() === 'POST')
      status.authors[0].enabled = Number(request.postDataJSON().enabled)
    if (path.endsWith('/authors') && request.method() === 'POST') {
      const body = request.postDataJSON()
      status.authors.push({
        id: 1,
        uid: '3962719063',
        platform: body.platform,
        name: body.name || '3962719063',
        enabled: 1,
        last_check: null,
        last_error: null,
      })
    }
    if (path.endsWith('/telegram-test'))
      return route.fulfill({ json: { message: '测试消息已发送，请检查 Telegram' } })
    return route.fulfill({ json: status })
  })
  await page.goto('/social-monitor')
  await expect(page.getByRole('heading', { name: '博主动态监控', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '监控用户', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '登录监控台' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '最新动态', exact: true })).toBeVisible()
  await expect(page.getByText('SOCIAL MONITOR', { exact: true })).toHaveCount(0)
  await page.getByRole('tab', { name: /监控博主/ }).click()
  await page.getByLabel('备注名称').fill('测试博主')
  await page.getByRole('button', { name: '添加用户' }).click()
  await expect(page.locator('.social-author strong[title]')).toHaveText('测试博主')
  await expect(page.getByText('UID 3962719063')).toHaveCount(0)
  await page.getByRole('tab', { name: '通知设置' }).click()
  await page.getByLabel('机器人密钥').fill('fake-token')
  await page.getByLabel('接收会话编号').fill('123')
  await page.getByRole('button', { name: '保存配置' }).click()
  await expect(page.getByRole('status')).toContainText('配置已保存')
  await expect(page.getByLabel('机器人密钥')).toHaveValue('')
  await page.getByRole('button', { name: '发送测试消息' }).click()
  await expect(page.getByRole('status')).toContainText('测试消息已发送')
  await page.getByRole('button', { name: '启动监控', exact: true }).click()
  await expect(page.getByText('后台监控中', { exact: true })).toBeVisible()
  await expect(page.getByText('10–16', { exact: false }).first()).toBeVisible()
  await page.getByRole('tab', { name: /监控博主/ }).click()
  await page.locator('.social-author').getByRole('button', { name: '暂停', exact: true }).click()
  await expect(page.locator('.social-author-reason')).toContainText('该博主已手动暂停')
  await page.locator('.social-author').getByRole('button', { name: '恢复', exact: true }).click()
  await page.getByRole('tab', { name: '最新动态' }).click()
  status.platforms.weibo = {
    paused: true,
    reason: '平台要求验证码',
    http_status: 403,
    paused_at: '2026-10-25T12:00:00+08:00',
    alert_sent_at: '2026-10-25T12:00:00+08:00',
  }
  await page.getByRole('button', { name: '刷新列表' }).click()
  await expect(page.getByRole('heading', { name: '微博：已自动暂停，等待人工处理' })).toBeVisible()
  await expect(page.locator('.social-pause-details')).toContainText('暂停原因')
  await expect(page.locator('.social-pause-details')).toContainText('平台要求验证码')
  await expect(page.locator('.social-pause-details')).toContainText('HTTP 403')
  await expect(page.locator('.social-pause-details')).toContainText('暂停时间')
  await page.getByRole('tab', { name: /监控博主/ }).click()
  await expect(page.locator('.social-author-reason')).toContainText('平台要求验证码')
  await page.getByRole('tab', { name: '最新动态' }).click()
  await page.getByRole('button', { name: '人工处理完成，恢复微博' }).click()
  await expect(page.getByRole('heading', { name: '微博：已自动暂停，等待人工处理' })).toHaveCount(0)
  showFeed = true
  await page.getByRole('button', { name: '刷新列表' }).click()
  await expect(page.getByText('今天发布的测试正文', { exact: false })).toBeVisible()
  await page.getByLabel('搜索帖子内容').fill('测试正文')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.getByText('今天发布的测试正文', { exact: false })).toBeVisible()
  await page.getByLabel('搜索帖子内容').fill('没有这个关键词')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.getByRole('heading', { name: '没有匹配的帖子' })).toBeVisible()
  await page.getByRole('button', { name: '清除搜索' }).click()
  await expect(page.getByText('今天发布的测试正文', { exact: false })).toBeVisible()
  await expect(page.getByText('<script>不会执行</script>', { exact: false })).toBeVisible()
  await expect(page.getByRole('link', { name: '查看原文' })).toHaveAttribute(
    'href',
    'https://m.weibo.cn/detail/123',
  )
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('tab', { name: '最新动态' }).focus()
  await page.keyboard.press('End')
  await expect(page.getByRole('tab', { name: '通知设置' })).toBeFocused()
  await expect(page.getByLabel('机器人密钥')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('social-settings.png'), fullPage: true })
  await page.getByRole('tab', { name: /监控博主/ }).click()
  await expect(page.locator('.social-author strong[title]')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('social-authors.png'), fullPage: true })
  await page.getByRole('tab', { name: '最新动态' }).click()
  await page.screenshot({ path: testInfo.outputPath('social-monitor.png'), fullPage: true })
  await page.reload()
  await expect(page.getByRole('heading', { name: '最新动态', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '登录监控台' })).toHaveCount(0)
  await expect(page.getByText('今天发布的测试正文', { exact: false })).toBeVisible()
  expect(postRequests).toBeGreaterThan(0)
  expect(errors).toEqual([])
})
