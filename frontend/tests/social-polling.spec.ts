import { expect, test } from '@playwright/test'

test('configure a random polling range without losing edits during status refresh', async ({
  page,
}, testInfo) => {
  await page.clock.install()
  const status = {
    enabled: false,
    interval_min: 600,
    interval_max: 960,
    last_check: null,
    next_check: null,
    last_error: null,
    token_configured: true,
    chat_id_configured: true,
    weibo_cookie_configured: true,
    xueqiu_cookie_configured: true,
    browser_login_available: false,
    authors: [],
    cubes: [],
    counts: {},
  }
  const changes: Record<string, unknown>[] = []
  let rejectSave = false
  let statusReads = 0
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/social-monitor/status')) {
      statusReads++
      return route.fulfill({ json: status })
    }
    if (path.endsWith('/social-monitor/posts'))
      return route.fulfill({ json: { total: 0, page: 1, items: [] } })
    if (path.endsWith('/social-monitor/config')) {
      const body = route.request().postDataJSON()
      changes.push(body)
      expect(Object.keys(body).sort()).toEqual(['interval_max', 'interval_min'])
      if (rejectSave)
        return route.fulfill({ status: 400, json: { detail: '轮询设置暂未保存，请重试' } })
      status.interval_min = body.interval_min
      status.interval_max = body.interval_max
      return route.fulfill({ json: status })
    }
    return route.fulfill({ status: 503, json: { detail: '未模拟的接口' } })
  })
  await page.goto('/social-monitor')
  await page.getByRole('tab', { name: '通知设置' }).click()
  const form = page.getByRole('form', { name: '平台轮询设置' })
  const minimum = form.getByLabel('最短周期（分钟）')
  const maximum = form.getByLabel('最长周期（分钟）')
  const save = form.getByRole('button', { name: '保存轮询设置' })
  const summary = page.getByText('平台轮询周期', { exact: true }).locator('..').locator('strong')
  await expect(minimum).toHaveValue('10')
  await expect(maximum).toHaveValue('16')
  await expect(save).toBeDisabled()

  for (const invalid of ['', '0', '1.5']) {
    await minimum.fill(invalid)
    await save.click()
    await expect(page.getByRole('alert')).toContainText('1～1440 之间的整数分钟')
  }
  for (const invalid of ['16', '20']) {
    await minimum.fill(invalid)
    await save.click()
    await expect(page.getByRole('alert')).toContainText('最短周期必须小于最长周期')
  }
  await minimum.fill('5')
  await maximum.fill('1441')
  await save.click()
  await expect(page.getByRole('alert')).toContainText('1～1440 之间的整数分钟')
  expect(changes).toHaveLength(0)

  await maximum.fill('9')
  status.interval_min = 1200
  status.interval_max = 1800
  const beforeRefresh = statusReads
  await page.clock.runFor(10001)
  await expect.poll(() => statusReads).toBeGreaterThan(beforeRefresh)
  await expect(summary).toHaveText('20–30 分钟')
  await expect(minimum).toHaveValue('5')
  await expect(maximum).toHaveValue('9')

  rejectSave = true
  await save.click()
  await expect(page.getByRole('alert')).toContainText('轮询设置暂未保存，请重试')
  await expect(summary).toHaveText('20–30 分钟')
  await expect(minimum).toHaveValue('5')
  await expect(maximum).toHaveValue('9')
  rejectSave = false
  await save.click()
  await expect(page.getByRole('status')).toContainText('已保存，下一轮轮询生效')
  await expect(summary).toHaveText('5–9 分钟')
  await expect(save).toBeDisabled()
  expect(changes).toEqual([
    { interval_min: 300, interval_max: 540 },
    { interval_min: 300, interval_max: 540 },
  ])

  await page.reload()
  await page.getByRole('tab', { name: '通知设置' }).click()
  await expect(minimum).toHaveValue('5')
  await expect(maximum).toHaveValue('9')
  await expect(summary).toHaveText('5–9 分钟')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('social-polling-settings.png'),
    fullPage: true,
  })
})
