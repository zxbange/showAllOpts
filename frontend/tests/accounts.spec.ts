import { test, expect, adminSession } from './fixtures'

test('site login protects deep links, does not persist passwords, and logout clears content', async ({
  page,
}) => {
  let authenticated = false
  await page.route('**/api/auth/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/users')) return route.fulfill({ json: { users: [adminSession.user] } })
    if (path.endsWith('/login')) {
      expect(route.request().postDataJSON()).toEqual({
        username: 'admin',
        password: 'test-long-password',
      })
      authenticated = true
    }
    if (path.endsWith('/logout')) {
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      authenticated = false
    }
    return route.fulfill({
      json: { ...adminSession, user: authenticated ? adminSession.user : null },
    })
  })
  await page.goto('/account')
  await expect(page.getByRole('heading', { name: '用户管理' })).toHaveCount(0)
  await page.getByLabel('用户名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-long-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '用户管理' })).toBeVisible()
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toMatch(/password|test-csrf/)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('')
  await expect(page.getByRole('heading', { name: '用户管理' })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('admin creates and disables a user with no separate module keys', async ({ page }) => {
  const users = [adminSession.user]
  await page.route('**/api/auth/users**', async (route) => {
    if (route.request().method() === 'POST') {
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      const body = route.request().postDataJSON()
      if (route.request().url().endsWith('/users')) {
        expect(body.role).toBe('viewer')
        users.push({ ...adminSession.user, id: 'reader', username: body.username, role: 'viewer' })
      } else users[1].enabled = body.enabled
      return route.fulfill({ json: users[1] })
    }
    return route.fulfill({ json: { users } })
  })
  await page.goto('/account')
  await page.getByLabel('用户名', { exact: true }).fill('reader')
  await page.getByLabel('初始密码').fill('unique-password-8927')
  await page.getByRole('button', { name: '创建用户', exact: true }).click()
  const row = page
    .getByRole('row')
    .filter({ has: page.getByRole('cell', { name: 'reader', exact: true }) })
  await expect(row).toBeVisible()
  await expect(page.getByLabel('初始密码')).toHaveValue('')
  page.once('dialog', (dialog) => dialog.accept())
  await row.getByRole('button', { name: '停用', exact: true }).click()
  await expect(row.getByRole('cell', { name: '停用', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('viewer has personal password settings but no user, sync or holdings controls', async ({
  page,
}) => {
  await page.route('**/api/auth/session', (route) =>
    route.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } } }),
  )
  await page.goto('/account')
  await expect(page.getByRole('heading', { name: '我的账号' })).toBeVisible()
  await expect(page.getByRole('button', { name: '创建用户' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '数据同步', exact: true })).toHaveCount(0)
  await page.goto('/personal-holdings')
  await expect(page.getByText('仅管理员可访问')).toBeVisible()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveCount(0)
})

test('HTTP setup does not allow entering passwords or registering the first admin online', async ({
  page,
}) => {
  await page.route('**/api/auth/session', (route) =>
    route.fulfill({ json: { user: null, configured: false, https_required: true } }),
  )
  await page.goto('/')
  await expect(page.getByText('请先在服务器终端创建管理员。')).toBeVisible()
  await expect(page.getByLabel('密码', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '注册' })).toHaveCount(0)
})
