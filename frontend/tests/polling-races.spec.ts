import { expect, test, type Route } from '@playwright/test'

test('a slow cache status response is not repeatedly cancelled by the polling timer', async ({
  page,
}) => {
  await page.route('**/api/v1/cache/status', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 4200))
    await route.fulfill({
      json: {
        enabled: true,
        refreshing: false,
        manual_enabled: true,
        auto_refresh: false,
        daily_time: '07:00',
        timezone: 'Asia/Shanghai',
        next_refresh_at: null,
        modules: [
          {
            id: 'industry-momentum',
            name: '行业动量',
            ready: true,
            updated_at: '2026-10-30T20:00:00+08:00',
            rows: 25,
            error: null,
          },
        ],
      },
    })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态', exact: true }).click()
  await expect(page.getByRole('dialog').getByText('行业动量', { exact: true }).first()).toBeVisible(
    { timeout: 12000 },
  )
  await expect(page.getByRole('dialog')).toContainText('已就绪')
})

test('late responses from old social filters cannot overwrite the current feed', async ({
  page,
}) => {
  let delayed: Route | undefined
  const status = {
    enabled: false,
    authors: [],
    cubes: [],
    counts: {},
    platforms: {},
    token_configured: false,
    chat_id_configured: false,
    last_check: null,
    next_check: null,
    interval_min: 600,
    interval_max: 960,
  }
  const feed = (label: string) => ({
    total: 1,
    page: 1,
    items: [
      {
        id: 1,
        platform: 'weibo',
        author_name: '测试博主',
        status: 'sent',
        sent_at: null,
        last_error: null,
        payload: {
          text: label,
          time: '2026-10-30T12:00:00+08:00',
          url: 'https://weibo.com/123/test',
        },
      },
    ],
  })
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/posts')) {
      const keyword = url.searchParams.get('keyword') || '初始动态'
      if (keyword === '旧筛选') {
        delayed = route
        return
      }
      return route.fulfill({ json: feed(keyword) })
    }
    return route.fulfill({
      json: url.pathname.includes('/social-monitor')
        ? status
        : { enabled: false, refreshing: false, modules: [] },
    })
  })
  await page.goto('/social-monitor')
  await expect(page.getByText('初始动态', { exact: true })).toBeVisible()
  const search = page.getByRole('textbox', { name: '搜索帖子内容' })
  await search.fill('旧筛选')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect.poll(() => Boolean(delayed)).toBe(true)
  await search.fill('新筛选')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.getByText('新筛选', { exact: true })).toBeVisible()
  await delayed!.fulfill({ json: feed('旧筛选') })
  await page.waitForTimeout(200)
  await expect(page.getByText('旧筛选', { exact: true })).toHaveCount(0)
  await expect(page.getByText('新筛选', { exact: true })).toBeVisible()
})
