import { test, expect } from './fixtures'
test('polling controls are centralized', async ({ page }) => {
  await page.goto('/social-monitor')
  await expect(page.getByRole('form', { name: '平台轮询设置' })).toHaveCount(0)
})
