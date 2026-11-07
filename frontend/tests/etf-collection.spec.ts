import { expect, test, adminSession } from './fixtures'
const configuration = {
  revision: 1,
  host: 'db.example',
  port: 3306,
  database: 'market',
  username: 'writer',
  password_set: true,
  enabled: false,
  lookback_days: 400,
  batch_days: 5,
  schedule: ['17:00'],
  source: 'page',
  transport_secure: true,
  db_tls: true,
}
const ready = { status: 'disabled', configured: true, enabled: false, manual: null, runs: [] }
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) =>
    r.fulfill({ json: configuration }),
  )
  await page.route('**/api/v1/etf-shares/collection-status', (r) => r.fulfill({ json: ready }))
  await page.route('**/api/v1/etf-shares/history?*', (r) =>
    r.fulfill({
      json: { group: 'hs300', group_name: '沪深300', end_date: null, funds: [], market_values: [] },
    }),
  )
  await page.route('**/api/v1/cache/status', (r) =>
    r.fulfill({ json: { enabled: false, modules: [] } }),
  )
})
test('edits configuration, retains password only for same account, and tests read-only', async ({
  page,
}) => {
  const bodies: Record<string, unknown>[] = []
  await page.route('**/api/v1/etf-shares/collection-configuration', async (r) => {
    if (r.request().method() === 'POST') {
      bodies.push(r.request().postDataJSON())
      expect(r.request().headers()['x-csrf-token']).toBe('test-csrf')
    }
    await r.fulfill({ json: { ...configuration, ...bodies.at(-1) } })
  })
  await page.route('**/api/v1/etf-shares/collection-test', (r) =>
    r.fulfill({ json: { ok: true, permissions_verified: false, message: '连接成功' } }),
  )
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await page.getByLabel('回溯天数').fill('500')
  await page.getByLabel('启用自动采集').check()
  await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
  await expect(page.getByText('采集配置已保存。')).toBeVisible()
  expect(bodies[0]).toMatchObject({ lookback_days: 500, enabled: true })
  expect(bodies[0]).not.toHaveProperty('password')
  await page.getByLabel('采集写入账号').fill('other')
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeDisabled()
  await page.getByLabel('数据库密码', { exact: true }).fill('test-only-password')
  await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
  expect(bodies[1].password).toBe('test-only-password')
  await page.getByRole('button', { name: '测试连接（只读）' }).click()
  await expect(page.getByText(/不代表写入权限验证通过/)).toBeVisible()
})
test('HTTP credentials are always blocked', async ({ page }) => {
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) =>
    r.fulfill({ json: { ...configuration, transport_secure: false } }),
  )
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await expect(page.getByText('请使用 HTTPS 后保存或测试凭据。')).toBeVisible()
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '测试连接（只读）' })).toBeDisabled()
  await expect(page.getByLabel('采集写入账号')).toBeDisabled()
  await expect(page.getByLabel('数据库密码', { exact: true })).toBeDisabled()
})
test('viewer sees status without fetching credentials or mutation buttons', async ({ page }) => {
  let reads = 0
  await page.route('**/api/auth/**', (r) =>
    r.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } } }),
  )
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
    reads++
    return r.fulfill({ json: configuration })
  })
  await page.goto('/etf-shares')
  await expect(page.getByText('自动采集已关闭，当前展示已保存的数据。')).toBeVisible()
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toHaveCount(0)
  await expect(page.getByText('采集配置', { exact: true })).toHaveCount(0)
  expect(reads).toBe(0)
})
test('uncertain manual submission reuses request id and repeated clicks are locked', async ({
  page,
}) => {
  const ids: string[] = []
  const choices: boolean[] = []
  await page.route('**/api/v1/etf-shares/collection-status', (r) =>
    r.fulfill({ json: { ...ready, retry_exhausted_count: 3 } }),
  )
  let finish!: () => void
  const hold = new Promise<void>((resolve) => {
    finish = resolve
  })
  await page.route('**/api/v1/etf-shares/collection-run', async (r) => {
    ids.push(r.request().postDataJSON().request_id)
    choices.push(r.request().postDataJSON().retry_exhausted)
    if (ids.length === 1) {
      await hold
      await r.abort()
    } else await r.fulfill({ status: 202, json: { id: ids[0], status: 'queued' } })
  })
  await page.goto('/etf-shares')
  await page.getByLabel('重新尝试已耗尽重试的数据（3项）').check()
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect(page.getByRole('button', { name: '提交中…' })).toBeDisabled()
  expect(ids).toHaveLength(1)
  finish()
  await expect(page.getByRole('button', { name: '重试同一采集请求' })).toBeVisible()
  await expect(page.getByLabel('重新尝试已耗尽重试的数据（3项）')).toBeDisabled()
  await page.getByRole('button', { name: '重试同一采集请求' }).click()
  await expect.poll(() => ids.length).toBe(2)
  expect(ids[0]).toBe(ids[1])
  expect(choices).toEqual([true, true])
})
test('polls active progress, refreshes chart after cache completion and stops on unmount', async ({
  page,
}) => {
  let polls = 0
  let histories = 0
  await page.clock.install()
  await page.route('**/api/v1/etf-shares/history?*', (r) => {
    histories++
    return r.fulfill({
      json: { group: 'hs300', group_name: '沪深300', end_date: null, funds: [], market_values: [] },
    })
  })
  await page.route('**/api/v1/etf-shares/collection-status', (r) => {
    polls++
    return r.fulfill({
      json: {
        ...ready,
        manual: { id: 'job-1', status: polls < 2 ? 'running' : 'complete' },
        cache_pending: polls === 2,
      },
    })
  })
  await page.goto('/etf-shares')
  await expect(page.getByText('手动采集：采集中', { exact: true })).toBeVisible()
  await page.clock.fastForward(2100)
  await expect(page.getByText('采集已入库，等待图表缓存更新…')).toBeVisible()
  expect(histories).toBe(1)
  await page.clock.fastForward(2100)
  await expect.poll(() => histories).toBe(2)
  await page.getByRole('link', { name: '发奋墙 首页', exact: true }).click()
  const count = polls
  await page.clock.fastForward(30000)
  expect(polls).toBe(count)
})

test('remote database without TLS requires explicit acknowledgment', async ({ page }) => {
  let body: Record<string, unknown> | null = null
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
    if (r.request().method() === 'POST') body = r.request().postDataJSON()
    return r.fulfill({ json: { ...configuration, db_local: false, db_tls: false } })
  })
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeDisabled()
  await page
    .getByLabel(
      '远程数据库未启用 TLS，凭据和数据可能被截获；我接受保存此配置后自动和手动采集均存在该风险',
    )
    .check()
  await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
  await expect(page.getByText('采集配置已保存。')).toBeVisible()
  expect(body).toMatchObject({ db_tls: false, db_transport_acknowledged: true })
})

test('late status from a departed page cannot replace new page status', async ({ page }) => {
  let calls = 0
  let release!: () => void
  const wait = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/etf-shares/collection-status', async (r) => {
    calls++
    if (calls === 1) {
      await wait
      await r
        .fulfill({ json: { ...ready, manual: { id: 'old', status: 'running' } } })
        .catch(() => {})
    } else await r.fulfill({ json: ready })
  })
  await page.goto('/etf-shares')
  await expect.poll(() => calls).toBe(1)
  await page.getByRole('link', { name: '发奋墙 首页', exact: true }).click()
  await page.getByRole('link', { name: '国家队动态', exact: true }).click()
  await expect(page.getByText('自动采集已关闭，当前展示已保存的数据。')).toBeVisible()
  release()
  await expect(page.getByText('手动采集：采集中', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
})

test('changed database target requires fresh password and explains least privilege', async ({
  page,
}) => {
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) =>
    r.fulfill({ json: { ...configuration, target_changed: true } }),
  )
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await expect(
    page.getByText('数据库目标已变更，旧密码不会沿用，请重新输入新目标的密码并保存。'),
  ).toBeVisible()
  await expect(page.getByText(/仅需 etf_share_daily/)).toBeVisible()
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeDisabled()
  await page.getByLabel('数据库密码', { exact: true }).fill('fresh-password-for-new-target')
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeEnabled()
})
