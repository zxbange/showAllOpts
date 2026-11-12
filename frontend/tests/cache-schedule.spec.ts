import { test, expect } from './fixtures'
test('legacy source reports upgrade requirement in sync status', async ({ page }) => {
  const note = '源库未启用新版变更日志，本次使用全量清单对账；升级独立及批量采集器后自动切换增量。'
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({
      json: {
        enabled: true,
        refreshing: false,
        manual_enabled: true,
        auto_refresh: true,
        daily_time: '07:00',
        timezone: 'Asia/Shanghai',
        modules: [
          {
            id: 'microcap-temperature',
            name: '微盘温度计',
            ready: true,
            updated_at: null,
            last_attempt_at: null,
            rows: 2,
            error: null,
            sync_note: note,
          },
        ],
      },
    }),
  )
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态' }).click()
  await expect(page.getByText(note)).toBeVisible()
})
