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
  await expect(page.getByLabel('数据库主机', { exact: true })).toBeDisabled()
  await expect(page.getByLabel('端口', { exact: true })).toBeDisabled()
  await expect(page.getByLabel('数据库名称', { exact: true })).toBeDisabled()
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
  // StrictMode may issue multiple initial reads. Only the test advances the job.
  let phase: 'running' | 'cache_pending' | 'complete' = 'running'
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
        manual: { id: 'job-1', status: phase === 'running' ? 'running' : 'complete' },
        cache_pending: phase === 'cache_pending',
      },
    })
  })
  await page.goto('/etf-shares')
  await expect(page.getByText('手动采集：采集中', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '暂无ETF份额数据', exact: true })).toBeVisible()
  const initialHistories = histories
  const initialPolls = polls
  phase = 'cache_pending'
  await page.clock.fastForward(2100)
  await expect(page.getByText('采集已入库，等待图表缓存更新…')).toBeVisible()
  expect(polls).toBeGreaterThan(initialPolls)
  expect(histories).toBe(initialHistories)
  phase = 'complete'
  await page.clock.fastForward(2100)
  await expect(page.getByText('采集已入库，等待图表缓存更新…')).toHaveCount(0)
  await expect.poll(() => histories).toBe(initialHistories + 1)
  await page.getByRole('link', { name: '总览', exact: true }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('region', { name: 'ETF采集管理' })).toHaveCount(0)
  const count = polls
  await page.clock.fastForward(30000)
  expect(polls).toBe(count)
})

test('remote database saves and tests without TLS options or acknowledgment', async ({ page }) => {
  const bodies: Record<string, unknown>[] = []
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
    if (r.request().method() === 'POST') bodies.push(r.request().postDataJSON())
    return r.fulfill({ json: configuration })
  })
  await page.route('**/api/v1/etf-shares/collection-test', (r) => {
    bodies.push(r.request().postDataJSON())
    return r.fulfill({ json: { ok: true, permissions_verified: false, message: '连接成功' } })
  })
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await expect(page.getByText(/TLS|我接受保存此配置/)).toHaveCount(0)
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
  await expect(page.getByText('采集配置已保存。')).toBeVisible()
  await page.getByRole('button', { name: '测试连接（只读）' }).click()
  await expect(page.getByText(/不代表写入权限验证通过/)).toBeVisible()
  expect(bodies).toHaveLength(2)
  for (const body of bodies) {
    expect(body).toMatchObject({ host: 'db.example', username: 'writer' })
    expect(body).not.toHaveProperty('db_tls')
    expect(body).not.toHaveProperty('db_transport_acknowledged')
  }
})

test('late status from a departed page cannot replace new page status', async ({ page }) => {
  let holdOldPage = true
  let heldResponses = 0
  let releasedResponses = 0
  let release!: () => void
  const wait = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/etf-shares/collection-status', async (r) => {
    // Hold every first-mount read, including StrictMode's duplicate request.
    if (holdOldPage) {
      heldResponses++
      await wait
      await r
        .fulfill({ json: { ...ready, manual: { id: 'old', status: 'running' } } })
        .catch(() => {}) // Aborting an unmounted page's fetch is expected.
      releasedResponses++
    } else await r.fulfill({ json: ready })
  })
  await page.goto('/etf-shares')
  await expect.poll(() => heldResponses).toBeGreaterThan(0)
  await expect(page.getByText('正在读取采集状态…', { exact: true })).toBeVisible()
  await page.getByRole('link', { name: '总览', exact: true }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('region', { name: 'ETF采集管理' })).toHaveCount(0)
  holdOldPage = false
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '市场情绪', exact: true })
    .click()
  await expect(page).toHaveURL(/etf-shares$/)
  await expect(page.getByText('自动采集已关闭，当前展示已保存的数据。')).toBeVisible()
  const oldResponses = heldResponses
  release()
  await expect.poll(() => releasedResponses).toBe(oldResponses)
  await expect(page.getByText('手动采集：采集中', { exact: true })).toHaveCount(0)
  await expect(page.getByText('自动采集已关闭，当前展示已保存的数据。')).toBeVisible()
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

test('existing account saves one adjustable daily time', async ({ page }) => {
  let body: Record<string, unknown> | undefined
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
    if (r.request().method() === 'POST') body = r.request().postDataJSON()
    return r.fulfill({ json: { ...configuration, username: 'app_reader', ...body } })
  })
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await expect(page.getByText('可使用现有具备权限的账号。')).toBeVisible()
  await expect(page.locator('input[type="time"]')).toHaveCount(1)
  await expect(page.getByLabel('每日采集时间（北京时间）')).toHaveValue('17:00')
  await page.getByLabel('每日采集时间（北京时间）').fill('08:45')
  await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
  await expect(page.getByText('采集配置已保存。')).toBeVisible()
  expect(body).toMatchObject({ username: 'app_reader', schedule: ['08:45'] })
  expect(body).not.toHaveProperty('password')
  await expect(page.getByRole('button', { name: '添加时间' })).toHaveCount(0)
})

for (const legacy of [
  { schedule: ['09:00', '17:00', '20:00'] },
  { schedule: ['09:00'], schedule_migrated: true },
]) {
  test(`merges legacy schedule and acknowledges saving (${legacy.schedule.length} slots)`, async ({
    page,
  }) => {
    let body: Record<string, unknown> | undefined
    await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
      if (r.request().method() === 'POST') body = r.request().postDataJSON()
      return r.fulfill({
        json: body
          ? { ...configuration, ...body, schedule_migrated: false }
          : { ...configuration, ...legacy },
      })
    })
    await page.goto('/etf-shares')
    await page.getByText('采集配置', { exact: true }).click()
    await expect(page.getByLabel('每日采集时间（北京时间）')).toHaveValue('09:00')
    await expect(page.getByText('旧时段已合并为每日一次，可调整时间并保存确认。')).toBeVisible()
    await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
    await expect(page.getByText('采集配置已保存。')).toBeVisible()
    expect(body?.schedule).toEqual(['09:00'])
    await expect(page.getByText('旧时段已合并为每日一次，可调整时间并保存确认。')).toHaveCount(0)
  })
}

test('reset discards unsaved settings and password', async ({ page }) => {
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await page.getByLabel('每日采集时间（北京时间）').fill('08:15')
  await page.getByLabel('数据库密码', { exact: true }).fill('unsaved-test-password')
  await page.getByRole('button', { name: '重置修改', exact: true }).click()
  await expect(page.getByLabel('每日采集时间（北京时间）')).toHaveValue('17:00')
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
})

test('configuration fits a narrow mobile viewport', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 740 })
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  const panel = page.getByRole('region', { name: 'ETF采集管理' })
  await expect(page.getByLabel('每日采集时间（北京时间）')).toBeVisible()
  expect(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  for (const element of await panel
    .locator('input:not([type="checkbox"]), .etf-collection-actions button')
    .all()) {
    const box = await element.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(320)
  }
  await panel.screenshot({ path: testInfo.outputPath('collection-mobile.png') })
})

test('custom database requires a fresh password and reloads ETF data', async ({ page }) => {
  let body: Record<string, unknown> | undefined
  let histories = 0
  await page.route('**/api/v1/etf-shares/history?*', (r) => {
    histories++
    return r.fulfill({
      json: { group: 'hs300', group_name: '沪深300', end_date: null, funds: [], market_values: [] },
    })
  })
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
    if (r.request().method() === 'POST') body = r.request().postDataJSON()
    return r.fulfill({ json: { ...configuration, ...body } })
  })
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await expect(page.getByRole('heading', { name: '暂无ETF份额数据', exact: true })).toBeVisible()
  const initialHistories = histories
  await page.getByLabel('数据库密码', { exact: true }).fill('password-for-old-target')
  await page.getByLabel('数据库主机', { exact: true }).fill('custom.example')
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
  await page.getByLabel('端口', { exact: true }).fill('3307')
  await page.getByLabel('数据库名称', { exact: true }).fill('etf_custom')
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeDisabled()
  await expect(page.getByText(/TLS|我接受保存此配置/)).toHaveCount(0)
  await page.getByLabel('数据库密码', { exact: true }).fill('fresh-test-password')
  await page.getByRole('button', { name: '保存采集配置', exact: true }).click()
  await expect(
    page.getByText('采集配置已保存。新数据库需重新同步ETF数据，其他模块不受影响。'),
  ).toBeVisible()
  expect(body).toMatchObject({
    host: 'custom.example',
    port: 3307,
    database: 'etf_custom',
    password: 'fresh-test-password',
  })
  await expect.poll(() => histories).toBeGreaterThan(initialHistories)
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
})

test('changing a local target to remote needs only fresh credentials, not a transport option', async ({
  page,
}) => {
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) =>
    r.fulfill({ json: { ...configuration, host: 'localhost', db_local: true } }),
  )
  await page.goto('/etf-shares')
  await page.getByText('采集配置', { exact: true }).click()
  await page.getByLabel('数据库主机', { exact: true }).fill('remote.example')
  await page.getByLabel('数据库名称', { exact: true }).fill('new_market')
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeDisabled()
  await page.getByLabel('数据库密码', { exact: true }).fill('fresh-test-password')
  await expect(page.getByRole('button', { name: '保存采集配置', exact: true })).toBeEnabled()
  await expect(page.getByText(/TLS|我接受保存此配置/)).toHaveCount(0)
})
