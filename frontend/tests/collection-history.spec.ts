import { test, expect } from './fixtures'

test('page footer shows only the latest five real runs with manual, automatic and cancellation semantics', async ({
  page,
}) => {
  await page.route('**/api/v1/growth-dividend/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '测试无业务快照' } }),
  )
  await page.route('**/api/v1/collection-outcomes/history?**', (route) => {
    const url = new URL(route.request().url())
    expect(url.searchParams.get('module')).toBe('growth-dividend')
    expect(url.searchParams.get('limit')).toBe('5')
    return route.fulfill({
      json: {
        items: Array.from({ length: 8 }, (_, index) => ({
          run_key: `run-${index}`,
          error_details:
            index === 1
              ? {
                  code: 'database_timeout',
                  message: '数据库操作超时',
                  stage: 'database_write',
                  mysql_errno: 2013,
                }
              : null,
          outcome: index === 0 ? 'success' : index === 1 ? 'failed' : 'cancelled',
          finished_at: `2026-11-18T${String(18 - index).padStart(2, '0')}:00:00+08:00`,
          trigger: index === 0 ? 'manual' : 'scheduled',
          kind: 'collection',
        })),
      },
    })
  })
  await page.goto('/growth-dividend')
  const panel = page.locator('[data-history-module="growth-dividend"]')
  await panel.getByText('最近采集记录', { exact: true }).click()
  await expect(panel.locator('li')).toHaveCount(5)
  await expect(panel.locator('li').nth(0)).toContainText('手动采集 · 成功')
  await expect(panel.locator('li').nth(1)).toContainText('自动采集 · 失败')
  await expect(panel.locator('li').nth(2)).toContainText('自动采集 · 已取消')
  await expect(panel.locator('li').nth(4)).toBeVisible()
  await panel.locator('li').nth(1).getByText('错误详情', { exact: true }).click()
  await expect(panel.locator('li').nth(1)).toContainText('数据库操作超时')
  await expect(panel.locator('li').nth(1)).toContainText('失败阶段：写入数据库')
  await expect(panel.locator('li').nth(1)).toContainText('MySQL 错误码：2013')
})

test('unavailable history is not presented as an empty completed journal', async ({ page }) => {
  await page.route('**/api/v1/growth-dividend/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '测试无业务快照' } }),
  )
  await page.route('**/api/v1/collection-outcomes/history?**', (route) =>
    route.fulfill({ status: 503, json: {} }),
  )
  await page.goto('/growth-dividend')
  const panel = page.locator('[data-history-module="growth-dividend"]')
  await panel.getByText('最近采集记录', { exact: true }).click()
  await expect(panel).toContainText('暂时无法读取最新采集记录')
  await expect(panel).not.toContainText('暂无已完成')
})

test('social feed combines both platforms into five recent rows and exposes partial source failures', async ({
  page,
}) => {
  await page.route('**/api/v1/social-monitor/status', (route) =>
    route.fulfill({
      json: {
        enabled: false,
        authors: [],
        counts: {},
        platforms: {},
        last_check: null,
        next_check: null,
      },
    }),
  )
  await page.route('**/api/v1/social-monitor/posts?**', (route) =>
    route.fulfill({ json: { total: 0, page: 1, items: [] } }),
  )
  let failXueqiu = false
  await page.route('**/api/v1/collection-outcomes/history?**', (route) => {
    const key = new URL(route.request().url()).searchParams.get('module')
    if (key === 'social-xueqiu' && failXueqiu) return route.fulfill({ status: 503, json: {} })
    return route.fulfill({
      json: {
        items: Array.from({ length: 5 }, (_, i) => ({
          run_key: String(i),
          outcome: i === 0 ? 'success' : 'failed',
          finished_at: `2026-11-18T${18 - i}:00:${key === 'social-xueqiu' ? '01' : '00'}+08:00`,
          trigger: 'scheduled',
          kind: 'collection',
        })),
      },
    })
  })
  await page.goto('/social-monitor')
  const panel = page.locator('[data-history-module="social-feed"]')
  await panel.getByText('微博 / 雪球最近采集记录', { exact: true }).click()
  await expect(panel.locator('li')).toHaveCount(5)
  await expect(panel.locator('li').nth(0)).toContainText('雪球')
  await expect(panel.locator('li').nth(1)).toContainText('微博')
  await expect(panel.locator('li').nth(2)).toContainText('失败')
  failXueqiu = true
  await page.reload()
  await panel.getByText('微博 / 雪球最近采集记录', { exact: true }).click()
  await expect(panel).toContainText('微博 / 雪球记录未能全部读取')
  await expect(panel.locator('li')).toHaveCount(0)
  await expect(panel).not.toContainText('暂无已完成')
})
