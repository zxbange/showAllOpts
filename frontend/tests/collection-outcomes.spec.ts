import { test, expect } from './fixtures'

test('shared collection lamps follow new completed results without changing tab names', async ({
  page,
}) => {
  let outcome = 'success'
  let unavailable = false
  await page.route('**/api/v1/collection-outcomes', (route) =>
    route.fulfill(
      unavailable
        ? { status: 503, json: {} }
        : {
            json: {
              items: {
                'dividend-selection': {
                  outcome,
                  finished_at: '2026-11-18T15:00:00+08:00',
                  trigger: 'manual',
                },
              },
            },
          },
    ),
  )
  await page.route('**/api/v1/dividend-selection/status', (route) =>
    route.fulfill({ json: { running: false, available: true } }),
  )
  await page.route('**/api/v1/dividend-selection/latest', (route) =>
    route.fulfill({ status: 503, json: { detail: '测试无快照' } }),
  )
  await page.goto('/dividend-selection')
  const bank = page.getByRole('link', { name: '高股息低估值（银行业）', exact: true })
  const nonbank = page.getByRole('link', { name: '高股息低估值（非银行业）', exact: true })
  await expect(bank.locator('.collection-outcome--success')).toBeVisible()
  await expect(nonbank.getByRole('img')).toHaveAttribute('title', /最近一次已结束手动采集：成功/)
  outcome = 'failed'
  await expect(bank.locator('.collection-outcome--failed')).toBeVisible({ timeout: 10000 })
  await bank.click()
  await expect(bank).toHaveAttribute('aria-current', 'page')
  await expect(nonbank.locator('.collection-outcome--failed')).toBeVisible()
  unavailable = true
  await expect(bank.getByRole('img')).toHaveAttribute('title', /状态暂不可用/, { timeout: 10000 })
  await expect(bank.locator('.collection-outcome--success')).toHaveCount(0)
})
