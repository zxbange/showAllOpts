import { expect, test } from './fixtures'

test('platform cards use monitor state, one scan, automatic completion, logout and cancellation', async ({
  page,
}) => {
  const status = {
    enabled: false,
    authors: [],
    counts: {},
    browser_login_available: true,
    token_configured: false,
    chat_id_configured: false,
    weibo_cookie_configured: false,
    xueqiu_cookie_configured: true,
    last_check: null,
    next_check: null,
  }
  type State = {
    id?: string
    state: string
    message: string
    image?: boolean
    session: { state: string }
  }
  const jobs: Record<string, State> = {
    weibo: { state: 'idle', message: '未登录', session: { state: 'logged_out' } },
    xueqiu: { state: 'success', message: '旧扫码结果', session: { state: 'normal' } },
  }
  let starts = 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    if (!path.includes('/social-monitor'))
      return route.fulfill({ status: 503, json: { detail: '测试' } })
    if (path.includes('/browser/')) {
      expect(request.headers()['x-browser-login-key']).toBeUndefined()
      if (request.method() === 'POST') expect(request.headers()['x-csrf-token']).toBe('test-csrf')
      const platform = path.includes('/xueqiu/') ? 'xueqiu' : 'weibo'
      if (path.endsWith('/start')) {
        starts++
        jobs[platform] = {
          id: 'a'.repeat(32),
          state: 'waiting',
          message: '等待扫码',
          image: true,
          session: { state: 'logged_out' },
        }
      } else if (path.endsWith('/logout')) {
        jobs[platform] = { state: 'idle', message: '未登录', session: { state: 'logged_out' } }
      } else if (path.endsWith('/action')) {
        expect(request.postDataJSON().action).toBe('cancel')
        jobs[platform] = { state: 'cancelled', message: '已取消', session: { state: 'logged_out' } }
      } else if (path.includes('/image/')) {
        return route.fulfill({
          contentType: 'image/png',
          body: Buffer.from(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jr1sAAAAASUVORK5CYII=',
            'base64',
          ),
        })
      }
      return route.fulfill({ json: jobs[platform] })
    }
    return route.fulfill({
      json: path.endsWith('/posts') ? { total: 0, page: 1, items: [] } : status,
    })
  })
  await page.goto('/social-monitor')
  await page.getByRole('tab', { name: '平台登录' }).click()
  const card = (name: string) =>
    page
      .locator('.social-browser-card')
      .filter({ has: page.getByRole('heading', { name, exact: true }) })
  const weibo = card('微博'),
    xueqiu = card('雪球')
  await expect(xueqiu.getByRole('status')).toHaveText('登录正常')
  await expect(xueqiu.getByRole('button', { name: '扫码登录', exact: true })).toHaveCount(0)
  await expect(xueqiu.getByRole('button', { name: '退出登录' })).toBeVisible()
  await expect(page.getByRole('button', { name: '隐藏登录窗口' })).toHaveCount(0)
  await weibo.getByRole('button', { name: '扫码登录', exact: true }).dblclick()
  await expect(weibo.getByAltText('微博官方登录画面')).toBeVisible()
  expect(starts).toBe(1)
  jobs.weibo = {
    id: 'a'.repeat(32),
    state: 'success',
    message: '登录正常',
    session: { state: 'normal' },
  }
  await expect(weibo.getByRole('status')).toHaveText('登录正常')
  await expect(weibo.getByAltText('微博官方登录画面')).toHaveCount(0)
  await expect(weibo.getByRole('button', { name: '扫码登录', exact: true })).toHaveCount(0)
  page.once('dialog', (dialog) => dialog.accept())
  await weibo.getByRole('button', { name: '退出登录' }).click()
  await expect(weibo.getByRole('status')).toHaveText('未登录')
  await weibo.getByRole('button', { name: '扫码登录', exact: true }).click()
  await expect(weibo.getByAltText('微博官方登录画面')).toBeVisible()
  await weibo.getByRole('button', { name: '取消扫码' }).click()
  await expect(weibo.getByAltText('微博官方登录画面')).toHaveCount(0)
  await expect(weibo.getByRole('button', { name: '扫码登录', exact: true })).toBeVisible()
  expect(starts).toBe(2)
  // A subsequent monitor failure overrides a historical successful QR login.
  jobs.xueqiu = { state: 'success', message: '旧扫码成功', session: { state: 'expired' } }
  await expect(xueqiu.getByRole('status')).toHaveText('登录已失效，请重新扫码')
  await expect(xueqiu.getByRole('button', { name: '扫码登录', exact: true })).toBeVisible()
})

test('a late status response cannot reopen a cancelled scan', async ({ page }) => {
  const initial = { state: 'idle', message: '未登录', session: { state: 'logged_out' } }
  let state: Record<string, unknown> = initial
  let hold = false,
    held = false
  let release: () => void = () => {}
  const delayed = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.includes('/social-monitor'))
      return route.fulfill({ status: 503, json: { detail: '测试' } })
    if (path.includes('/browser/xueqiu/')) return route.fulfill({ json: initial })
    if (path.includes('/browser/weibo/')) {
      if (path.endsWith('/start')) {
        state = {
          id: 'b'.repeat(32),
          state: 'waiting',
          message: '等待扫码',
          image: false,
          session: { state: 'logged_out' },
        }
      } else if (path.endsWith('/action')) state = initial
      const snapshot = state
      if (path.endsWith('/status') && hold) {
        hold = false
        held = true
        await delayed
      }
      return route.fulfill({ json: snapshot })
    }
    return route.fulfill({
      json: path.endsWith('/posts')
        ? { total: 0, page: 1, items: [] }
        : {
            enabled: false,
            authors: [],
            counts: {},
            browser_login_available: true,
          },
    })
  })
  try {
    await page.goto('/social-monitor')
    await page.getByRole('tab', { name: '平台登录' }).click()
    const weibo = page
      .locator('.social-browser-card')
      .filter({ has: page.getByRole('heading', { name: '微博', exact: true }) })
    await weibo.getByRole('button', { name: '扫码登录', exact: true }).click()
    hold = true
    await expect.poll(() => held, { timeout: 10000 }).toBe(true)
    await weibo.getByRole('button', { name: '取消扫码' }).click()
    await expect(weibo.getByRole('status')).toHaveText('未登录')
    release()
    await expect(weibo.getByRole('button', { name: '扫码登录', exact: true })).toBeVisible()
    await expect(weibo.getByRole('button', { name: '取消扫码' })).toHaveCount(0)
  } finally {
    release()
  }
})
