import { test as base } from '@playwright/test'
export { expect } from '@playwright/test'
export type { Page, Route } from '@playwright/test'

export const adminSession = {
  user: { id: 'admin', username: 'admin', role: 'admin', enabled: true, created_at: 0 },
  csrf_token: 'test-csrf',
  configured: true,
  https_required: false,
}

// Feature regressions run as a signed-in administrator. accounts.spec.ts exercises
// anonymous/login/read-only/revoked states explicitly by overriding this mock.
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route('**/api/auth/**', (route) => route.fulfill({ json: adminSession }))
    await use(page)
  },
})
