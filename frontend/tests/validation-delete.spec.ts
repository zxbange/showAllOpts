import { expect, test } from '@playwright/test'

test('delete requires confirmation, handles conflicts, removes only selected job and survives refresh', async ({
  page,
}, testInfo) => {
  let jobs = ['first', 'second'].map((id) => ({
    id,
    methods: ['bukou'],
    start: '2024-01-01',
    end: '2025-01-01',
    observed: '2025-01-07',
    status: 'completed',
    stage: '完成',
    done: 1,
    total: 1,
    captured: 1,
    updated_at: '2026-10-30T00:00:00Z',
    version: 'four-close-v1',
    summary: [],
    windows: [],
    error: null,
  }))
  let calls = 0
  let conflict = true
  let holdDelete = false
  let releaseDelete: (() => void) | undefined
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { enabled: false, modules: [] } }))
  await page.route('**/api/v1/short-term/validation/**', async (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({
        json: { mode: 'pair', authenticated: true, csrf_token: 'test-csrf', https_required: false },
      })
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [] } })
    if (path.endsWith('/delete')) {
      calls++
      expect(r.request().method()).toBe('POST')
      expect(r.request().headers()['x-validation-csrf']).toBe('test-csrf')
      expect(r.request().headers()['origin']).toBe('http://127.0.0.1:5173')
      if (conflict)
        return r.fulfill({ status: 409, json: { detail: '后台工作尚未退出，请稍后重试' } })
      if (holdDelete)
        await new Promise<void>((resolve) => {
          releaseDelete = resolve
        })
      jobs = jobs.filter((j) => !path.endsWith('/' + j.id + '/delete'))
      return r.fulfill({ json: { deleted: true } })
    }
    return r.fulfill({ json: { jobs } })
  })
  await page.goto('/short-term/validation')
  const triggers = page.getByRole('button', { name: '删除记录', exact: true })
  await expect(triggers).toHaveCount(2)
  await triggers.first().focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '删除这条验证记录？' })
  await expect(dialog).toContainText('不可恢复')
  await expect(dialog).toContainText('源数据库行情、共享连接配置及其他任务不受影响')
  await expect(dialog.getByRole('button', { name: '保留记录' })).toBeFocused()
  await expect(dialog.getByRole('button', { name: '永久删除记录和数据' })).toHaveCSS(
    'color',
    'rgb(255, 255, 255)',
  )
  await page.screenshot({ path: testInfo.outputPath('delete-confirmation.png') })
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  expect(calls).toBe(0)
  await expect(triggers.first()).toBeFocused()
  await triggers.first().click()
  await dialog.getByRole('button', { name: '保留记录' }).click()
  expect(calls).toBe(0)
  await triggers.first().click()
  await dialog.getByRole('button', { name: '永久删除记录和数据' }).click()
  await expect(dialog.getByRole('alert')).toContainText('后台工作尚未退出')
  await expect(triggers).toHaveCount(2)
  conflict = false
  holdDelete = true
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await dialog.getByRole('button', { name: '永久删除记录和数据' }).click()
  await expect.poll(() => !!releaseDelete).toBe(true)
  await expect(dialog.getByRole('button', { name: '正在删除…' })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeVisible()
  expect(calls).toBe(2)
  holdDelete = false
  releaseDelete!()
  await expect(dialog).not.toBeVisible()
  await expect(triggers).toHaveCount(1)
  await expect(page.getByRole('heading', { name: '验证记录', exact: true })).toBeFocused()
  expect(jobs[0].id).toBe('second')
  await page.reload()
  await expect(triggers).toHaveCount(1)
  await triggers.click()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '永久删除记录和数据' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByText('从一次验证开始', { exact: true })).toBeVisible()
  await page.reload()
  await expect(triggers).toHaveCount(0)
})

test('running and cancelling cannot delete; late polling cannot resurrect a deleted record', async ({
  page,
}) => {
  const job = {
    id: 'test',
    methods: ['bukou'],
    start: '2024-01-01',
    status: 'running',
    stage: '计算',
    captured: 1,
    done: 0,
    total: 1,
    summary: [],
    windows: [],
    updated_at: '2026-10-30T00:00:00Z',
  }
  let deleted = false
  let stale = false
  let release: (() => void) | undefined
  let calls = 0
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { enabled: false, modules: [] } }))
  await page.route('**/api/v1/short-term/validation/**', async (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({
        json: { mode: 'pair', authenticated: true, csrf_token: 'test', https_required: false },
      })
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [] } })
    if (path.endsWith('/cancel')) {
      job.status = 'cancelling'
      return r.fulfill({ json: job })
    }
    if (path.endsWith('/delete')) {
      calls++
      deleted = true
      return r.fulfill({ json: { deleted: true } })
    }
    const snapshot = deleted ? [] : [{ ...job }]
    if (stale) {
      stale = false
      await new Promise<void>((resolve) => {
        release = resolve
      })
    }
    return r.fulfill({ json: { jobs: snapshot } })
  })
  await page.goto('/short-term/validation')
  const trigger = page.getByRole('button', { name: '删除记录', exact: true })
  await expect(trigger).toBeDisabled()
  await page.getByRole('button', { name: '取消任务（不可继续）' }).click()
  await expect(trigger).toBeDisabled()
  job.status = 'cancelled'
  await expect(trigger).toBeEnabled({ timeout: 6000 })
  stale = true
  await expect.poll(() => !!release, { timeout: 6000 }).toBe(true)
  await trigger.click()
  const confirm = page.getByRole('button', { name: '永久删除记录和数据' })
  await confirm.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog')).not.toBeVisible()
  release!()
  await expect(trigger).toHaveCount(0)
  await expect(page.getByText('从一次验证开始', { exact: true })).toBeVisible()
  await page.waitForTimeout(500)
  await expect(trigger).toHaveCount(0)
  expect(calls).toBe(1)
})
