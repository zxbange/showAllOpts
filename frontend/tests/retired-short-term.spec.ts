import { test, expect } from './fixtures'

test('retired routes show not found and navigation retains other modules', async ({
  page,
}, info) => {
  await page.route('**/api/v1/**', (route) => route.fulfill({ json: { modules: [] } }))
  for (const path of ['/short-term', '/short-term/validation']) {
    await page.goto(path)
    await expect(page.getByRole('heading', { name: '页面没有找到' })).toBeVisible()
    await expect(page.getByRole('link', { name: '短线选股', exact: true })).toHaveCount(0)
    await page.reload()
    await expect(page.getByRole('heading', { name: '页面没有找到' })).toBeVisible()
  }
  await page.goto('/')
  await expect(page.getByRole('link', { name: /大宗商品/ }).first()).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: `/workspace/removal-preview/${info.project.name}-navigation.png`,
    fullPage: true,
  })
})
