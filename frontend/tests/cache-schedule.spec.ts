import { expect, test } from '@playwright/test'

const initial = {
  enabled: true,
  refreshing: false,
  manual_enabled: true,
  auto_refresh: true,
  daily_time: '07:00',
  timezone: 'Asia/Shanghai',
  next_refresh_at: '2026-10-30T07:00:00+08:00',
  modules: [],
}

test('edit, save, reload schedule without triggering sync; polling preserves edits', async ({
  page,
}) => {
  let status = { ...initial }
  let saves = 0
  let syncs = 0
  await page.route('**/api/v1/cache/status', (route) => route.fulfill({ json: status }))
  await page.route('**/api/v1/cache/refresh', (route) => {
    syncs++
    return route.fulfill({ status: 202, json: { accepted: true } })
  })
  await page.route('**/api/v1/cache/schedule', (route) => {
    saves++
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({ daily_time: '08:15' })
    status = { ...status, daily_time: '08:15', next_refresh_at: '2026-10-30T08:15:00+08:00' }
    return route.fulfill({ json: status })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态' }).click()
  const time = page.getByLabel('每日同步时间（北京时间）')
  await expect(time).toHaveValue('07:00')
  await expect(page.getByText('当前设置：每天 07:00，北京时间（Asia/Shanghai）。')).toBeVisible()
  await time.fill('08:15')
  const poll = page.waitForResponse('**/api/v1/cache/status')
  await poll
  await expect(time).toHaveValue('08:15')
  await page.getByRole('button', { name: '保存同步时间' }).click()
  await expect(page.getByText('同步时间已保存，不会立即触发同步。')).toBeVisible()
  await expect(page.getByText('当前设置：每天 08:15，北京时间（Asia/Shanghai）。')).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '数据同步状态' }).click()
  await expect(time).toHaveValue('08:15')
  expect(saves).toBe(1)
  expect(syncs).toBe(0)
  expect(await page.getByRole('dialog').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(
    true,
  )
})

test('failed save retains active time and edit, supports retry during running sync', async ({
  page,
}) => {
  let failing = true
  let active = { ...initial, refreshing: true }
  await page.route('**/api/v1/cache/status', (route) => route.fulfill({ json: active }))
  await page.route('**/api/v1/cache/schedule', (route) => {
    if (failing)
      return route.fulfill({
        status: 503,
        json: { detail: '无法读写同步时间设置，请检查服务存储权限后重试。' },
      })
    active = { ...active, daily_time: '23:59' }
    return route.fulfill({ json: active })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态' }).click()
  const time = page.getByLabel('每日同步时间（北京时间）')
  await time.fill('23:59')
  await page.getByRole('button', { name: '保存同步时间' }).click()
  await expect(page.getByRole('alert')).toContainText('无法读写同步时间设置')
  await expect(time).toHaveValue('23:59')
  await expect(page.getByText('当前设置：每天 07:00，北京时间（Asia/Shanghai）。')).toBeVisible()
  failing = false
  await page.getByRole('button', { name: '保存同步时间' }).click()
  await expect(page.getByText('当前设置：每天 23:59，北京时间（Asia/Shanghai）。')).toBeVisible()
  await expect(page.getByRole('button', { name: '同步进行中…', exact: true })).toBeDisabled()
})

test('loading does not offer an unverified schedule; empty input cannot submit', async ({
  page,
}) => {
  let release!: () => void
  const ready = new Promise<void>((resolve) => {
    release = resolve
  })
  let saves = 0
  await page.route('**/api/v1/cache/status', async (route) => {
    await ready
    return route.fulfill({ json: { ...initial, auto_refresh: false, next_refresh_at: null } })
  })
  await page.route('**/api/v1/cache/schedule', (route) => {
    saves++
    return route.fulfill({ json: initial })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '数据同步状态' }).click()
  await expect(page.getByText('正在读取同步状态…')).toBeVisible()
  await expect(page.getByLabel('每日同步时间（北京时间）')).toHaveCount(0)
  release()
  const time = page.getByLabel('每日同步时间（北京时间）')
  await expect(time).toHaveValue('07:00')
  await expect(page.getByText('自动同步已关闭；保存时间不会开启自动同步。')).toBeVisible()
  await time.fill('')
  await page.getByRole('button', { name: '保存同步时间' }).click()
  expect(await time.evaluate((el: HTMLInputElement) => el.validity.valueMissing)).toBe(true)
  expect(saves).toBe(0)
})

test('legacy source reports upgrade requirement in sync status', async ({ page }) => {
  const note = '源库未启用新版变更日志，本次使用全量清单对账；升级独立及批量采集器后自动切换增量。'
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({
      json: {
        ...initial,
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
