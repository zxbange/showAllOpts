import { expect, test } from '@playwright/test'

test('server login protects its requests and updates cookie status after confirmation', async ({
  page,
}) => {
  const key = 'browser-login-test-key-123456789'
  const status = {
    enabled: false,
    authors: [],
    counts: {},
    browser_login_available: true,
    token_configured: false,
    chat_id_configured: false,
    weibo_cookie_configured: false,
    xueqiu_cookie_configured: false,
    last_check: null,
    next_check: null,
  }
  const jobs: Record<string, { id?: string; state: string; message: string; image?: boolean }> = {
    weibo: { state: 'idle', message: '尚未打开登录窗口' },
    xueqiu: { state: 'idle', message: '尚未打开登录窗口' },
  }
  let images = 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (!url.pathname.includes('/social-monitor'))
      return route.fulfill({ status: 503, json: { detail: '测试只模拟监控模块' } })
    if (url.pathname.includes('/browser/')) {
      expect(url.search).not.toContain(key)
      expect(request.headers()['x-browser-login-key']).toBe(key)
      const platform = url.pathname.includes('/xueqiu/') ? 'xueqiu' : 'weibo'
      if (url.pathname.endsWith('/start')) {
        jobs[platform] = {
          id: 'a'.repeat(32),
          state: 'waiting',
          message: '等待扫码确认',
          image: true,
        }
      } else if (url.pathname.endsWith('/action')) {
        const payload = request.postDataJSON()
        expect(payload.id).toBe('a'.repeat(32))
        if (payload.action === 'cancel')
          jobs[platform] = { state: 'cancelled', message: '登录已取消' }
        else if (payload.action === 'check') {
          jobs[platform] = {
            id: 'a'.repeat(32),
            state: 'success',
            message: '登录已验证，凭据已加密保存',
          }
          status.weibo_cookie_configured = true
        } else if (payload.action === 'click') {
          expect(payload.x).toBeGreaterThanOrEqual(0)
          expect(payload.x).toBeLessThan(1100)
        }
      } else if (url.pathname.includes('/image/')) {
        images++
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
      json: url.pathname.endsWith('/posts') ? { total: 0, page: 1, items: [] } : status,
    })
  })
  await page.goto('/social-monitor')
  await page.getByRole('tab', { name: '通知设置' }).click()
  await expect(page.getByRole('heading', { name: '扫码登录', exact: true })).toHaveCount(0)
  await page.getByLabel('浏览器登录管理密钥', { exact: true }).fill(key)
  await page.getByRole('button', { name: '查看登录状态' }).click()
  const weibo = page
    .locator('.social-browser-card')
    .filter({ has: page.getByRole('heading', { name: '微博', exact: true }) })
  await weibo.getByRole('button', { name: '打开扫码登录' }).click()
  await expect(weibo.getByAltText('微博官方登录画面')).toBeVisible()
  expect(images).toBeGreaterThan(0)
  await weibo.getByRole('button', { name: '检查登录结果' }).click()
  await expect(weibo.getByRole('status')).toContainText('登录已验证')
  await expect(weibo.getByAltText('微博官方登录画面')).toHaveCount(0)
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(key)
  expect(await page.evaluate(() => JSON.stringify(sessionStorage))).not.toContain(key)
  await page.getByRole('button', { name: '隐藏登录窗口' }).click()
  await expect(page.getByLabel('浏览器登录管理密钥', { exact: true })).toHaveValue('')
})
