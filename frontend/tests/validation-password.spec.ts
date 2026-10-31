import { expect, test } from '@playwright/test'

test('saved password indicator preserves secrets; probe and start use named connection', async ({
  page,
}) => {
  const source = {
    id: 'a'.repeat(32),
    revision: 3,
    name: '研究库',
    host: 'db.example',
    port: 3306,
    database: 'research',
    username: 'reader',
    tls: 'verify',
    ca_pem: '',
    password_set: true,
  }
  let saves = 0
  const passwords: unknown[] = []
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], dates: [], counts: {} } }),
  )
  await page.route('**/api/v1/short-term/validation/**', (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({ json: { mode: 'pair', authenticated: true, csrf_token: 'mock' } })
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [source] } })
    if (path.endsWith('/sources/test') || path.endsWith('/sources/save')) {
      passwords.push(r.request().postDataJSON().password)
      if (path.endsWith('/test')) return r.fulfill({ json: { message: '只读测试成功' } })
      saves++
      return r.fulfill({ json: source })
    }
    if (r.request().method() === 'POST') {
      expect(r.request().postDataJSON().source).toEqual({ id: source.id, revision: 3 })
      return r.fulfill({
        json: {
          id: 'job',
          ...r.request().postDataJSON(),
          source_name: '研究库',
          version: 'four-close-v1',
          input_mode: 'static-batch-v1',
          status: 'queued',
          stage: '等待',
          done: 0,
          total: 0,
          captured: 0,
          summary: [],
          windows: [],
        },
      })
    }
    return r.fulfill({ json: { jobs: [] } })
  })
  await page.goto('/short-term/validation')
  const picker = page.getByLabel('验证数据源')
  await picker.selectOption(`${source.id}:3`)
  await expect(picker.locator('option:checked')).toHaveText('研究库')
  const password = page.getByLabel('数据库密码', { exact: true })
  await expect(password).toHaveAttribute('placeholder', '********')
  await expect(password).toHaveValue('')
  await password.focus()
  await password.blur()
  await page.getByRole('button', { name: '测试连接' }).click()
  await expect(page.getByRole('button', { name: '测试连接', exact: true })).toHaveClass(
    /probe-success/,
  )
  expect(saves).toBe(0)
  expect(passwords).toEqual([null])
  await password.fill('********') // actual user input, not the placeholder
  await page.getByRole('button', { name: '测试连接' }).click()
  await expect.poll(() => passwords.length).toBe(2)
  expect(passwords[1]).toBe('********')
  await password.fill('new-secret')
  await picker.selectOption('')
  await expect(password).toHaveValue('')
  await expect(password).not.toHaveAttribute('placeholder', '********')
  await picker.selectOption(`${source.id}:3`)
  await password.fill('same-secret')
  await page.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect.poll(() => saves).toBe(1)
  expect(passwords[2]).toBe('same-secret')
  await page.getByText('任务参数', { exact: true }).click()
  await expect(page.getByText('数据源：研究库')).toBeVisible()
  await page.getByText('配置新的验证任务', { exact: false }).click()
  await expect(password).toHaveAttribute('placeholder', '********')
  await password.fill('to-clear')
  await password.fill('')
  await page.getByRole('button', { name: '测试连接' }).click()
  await expect.poll(() => passwords.length).toBe(4)
  expect(passwords[3]).toBeNull()
  await expect(page.getByRole('button', { name: '清空并新增' })).toHaveCount(0)
  await picker.selectOption('')
  await expect(password).not.toHaveAttribute('placeholder', '********')
  await expect(password).toHaveValue('')
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toMatch(/same-secret|new-secret/)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('probe state resets on edits and ignores late success and failure', async ({ page }) => {
  let release: (() => void) | undefined
  let fail = false
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], dates: [], counts: {} } }),
  )
  await page.route('**/api/v1/short-term/validation/**', async (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({ json: { mode: 'pair', authenticated: true, csrf_token: 'mock' } })
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [] } })
    if (path.endsWith('/sources/test')) {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return r.fulfill(
        fail
          ? { status: 409, json: { detail: '连接失败：请检查网络和TLS设置' } }
          : { json: { message: '连接及三张研究表字段读取测试通过（只读）' } },
      )
    }
    return r.fulfill({ json: { jobs: [] } })
  })
  await page.goto('/short-term/validation')
  const button = page.getByRole('button', { name: '测试连接', exact: true })
  await expect(button).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  const host = page.getByLabel('数据库地址', { exact: true })
  await host.fill('db.example')
  for (const failure of [false, true]) {
    fail = failure
    release = undefined
    await button.click()
    await expect(button).toBeDisabled()
    await expect.poll(() => !!release).toBe(true)
    await host.fill(failure ? 'other2.example' : 'other1.example')
    release!()
    await expect(button).toBeEnabled()
    await expect(button).toHaveClass(/probe-idle/)
    await expect(page.getByText('连接失败：请检查网络和TLS设置')).toHaveCount(0)
  }
  release = undefined
  await button.click()
  await expect.poll(() => !!release).toBe(true)
  release!()
  await expect(button).toHaveClass(/probe-error/)
  await expect(button).toHaveCSS('color', 'rgb(185, 28, 28)')
  await expect(page.getByText('连接失败：请检查网络和TLS设置')).toBeVisible()
  await host.fill('db.example')
  await expect(button).toHaveClass(/probe-idle/)
  fail = false
  release = undefined
  await button.click()
  await expect.poll(() => !!release).toBe(true)
  release!()
  await expect(button).toHaveClass(/probe-success/)
  await expect(button).toHaveCSS('color', 'rgb(22, 101, 52)')
  await expect(page.getByText('连接及三张研究表字段读取测试通过（只读）')).toHaveCount(0)
  await page.getByLabel('数据库密码', { exact: true }).fill('changed')
  await expect(button).toHaveClass(/probe-idle/)
})

test('save by new name retains old profile and requires explicit password for changed target', async ({
  page,
}) => {
  const original = {
    id: 'a'.repeat(32),
    revision: 1,
    name: '原库',
    host: 'db.example',
    port: 3306,
    database: 'research',
    username: 'reader',
    tls: 'verify',
    ca_pem: '',
    password_set: true,
  }
  let saves = 0
  let probes = 0
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], jobs: [], dates: [] } }),
  )
  await page.route('**/validation/access', (r) =>
    r.fulfill({ json: { mode: 'pair', authenticated: true, csrf_token: 'mock' } }),
  )
  await page.route('**/validation/sources', (r) => r.fulfill({ json: { sources: [original] } }))
  await page.route('**/validation/sources/test', (r) => {
    probes++
    return r.fulfill({ json: { ok: true } })
  })
  await page.route('**/validation/sources/save', (r) => {
    saves++
    const body = r.request().postDataJSON()
    expect(body.name).toBe('新库')
    expect(body.password).toBe('explicit-new-password')
    const { password: _, ...fields } = body
    return r.fulfill({ json: { ...fields, id: 'b'.repeat(32), revision: 1, password_set: true } })
  })
  await page.goto('/short-term/validation')
  const picker = page.getByLabel('验证数据源')
  await picker.selectOption(`${original.id}:1`)
  const password = page.getByLabel('数据库密码', { exact: true })
  await expect(password).toHaveAttribute('placeholder', '********')
  await page.getByLabel('配置名称', { exact: true }).fill('新库')
  await expect(password).not.toHaveAttribute('placeholder', '********')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('数据库名称或连接信息已变更，请重新输入密码')).toBeVisible()
  expect(saves).toBe(0)
  await password.fill('explicit-new-password')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(picker).toHaveValue(`${'b'.repeat(32)}:1`)
  await expect(picker.getByRole('option', { name: '原库', exact: true })).toHaveCount(1)
  await expect(picker.getByRole('option', { name: '新库', exact: true })).toHaveCount(1)
  await page.getByLabel('数据库地址', { exact: true }).fill('other.example')
  await page.getByRole('button', { name: '测试连接', exact: true }).click()
  await expect(page.getByText('数据库名称或连接信息已变更，请重新输入密码')).toBeVisible()
  expect(probes).toBe(0)
  await expect(page.getByRole('button', { name: '清空并新增' })).toHaveCount(0)
})
