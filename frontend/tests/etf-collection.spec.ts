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
test('viewer has no empty collection panel, credentials or mutation buttons', async ({ page }) => {
  let reads = 0
  await page.route('**/api/auth/**', (r) =>
    r.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } } }),
  )
  await page.route('**/api/v1/etf-shares/collection-configuration', (r) => {
    reads++
    return r.fulfill({ json: configuration })
  })
  await page.goto('/etf-shares')
  await expect(page.getByRole('heading', { name: '暂无ETF份额数据', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'ETF采集管理' })).toHaveCount(0)
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
  await page.getByLabel('重新尝试当前目标日已耗尽重试的数据（3项）').check()
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect(page.getByRole('button', { name: '提交中…' })).toBeDisabled()
  expect(ids).toHaveLength(1)
  finish()
  await expect(page.getByRole('button', { name: '重试同一采集请求' })).toBeVisible()
  await expect(page.getByLabel('重新尝试当前目标日已耗尽重试的数据（3项）')).toBeDisabled()
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
  await expect(page.getByRole('button', { name: '取消采集', exact: true })).toBeEnabled()
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
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeDisabled()
  await page.getByRole('link', { name: '总览', exact: true }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('region', { name: 'ETF采集管理' })).toHaveCount(0)
  holdOldPage = false
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '市场情绪', exact: true })
    .click()
  await expect(page).toHaveURL(/etf-shares$/)
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
  const oldResponses = heldResponses
  release()
  await expect.poll(() => releasedResponses).toBe(oldResponses)
  await expect(page.getByText('手动采集：采集中', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
})

test('manual runs remain cancellable and each new round gets a fresh id', async ({ page }) => {
  const ids: string[] = []
  const cancelled: string[] = []
  let manual: { id: string; status: string } | null = null
  let release!: () => void
  const hold = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.clock.install()
  await page.route('**/api/v1/etf-shares/collection-status', (r) =>
    r.fulfill({ json: { ...ready, manual } }),
  )
  await page.route('**/api/v1/etf-shares/collection-run', (r) => {
    ids.push(r.request().postDataJSON().request_id)
    manual = { id: ids.at(-1)!, status: 'queued' }
    return r.fulfill({ status: 202, json: manual })
  })
  await page.route('**/api/v1/etf-shares/collection-cancel', async (r) => {
    cancelled.push(r.request().postDataJSON().request_id)
    expect(r.request().headers()['x-csrf-token']).toBe('test-csrf')
    await hold
    manual = { id: cancelled.at(-1)!, status: 'cancelling' }
    await r.fulfill({ json: manual })
  })
  await page.goto('/etf-shares')
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  const cancel = page.getByRole('button', { name: '取消采集', exact: true })
  await expect(cancel).toBeEnabled()
  await cancel.click()
  await expect(cancel).toBeDisabled()
  release()
  await expect(page.getByText('正在等待当前采集步骤结束，取消后可重新开始。')).toBeVisible()
  await expect(cancel).toBeEnabled()
  await cancel.click()
  await cancel.click()
  expect(cancelled).toEqual([ids[0]])
  manual = { id: ids[0], status: 'cancelled' }
  await page.clock.fastForward(2100)
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect(cancel).toBeEnabled()
  expect(ids).toHaveLength(2)
  expect(ids[1]).not.toBe(ids[0])
  manual = { id: ids[1], status: 'complete' }
  await page.clock.fastForward(2100)
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect(cancel).toBeEnabled()
  expect(new Set(ids).size).toBe(3)
})

test('an uncertain start resolves through polling without reusing its id for a new round', async ({
  page,
}) => {
  const ids: string[] = []
  let manual: { id: string; status: string } | null = null
  await page.route('**/api/v1/etf-shares/collection-status', (r) =>
    r.fulfill({ json: { ...ready, manual } }),
  )
  await page.route('**/api/v1/etf-shares/collection-run', (r) => {
    ids.push(r.request().postDataJSON().request_id)
    manual = { id: ids.at(-1)!, status: 'complete' }
    return r.abort()
  })
  await page.goto('/etf-shares')
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect.poll(() => ids.length).toBe(2)
  expect(ids[0]).not.toBe(ids[1])
})

test('automatic collection cannot be cancelled as a manual task', async ({ page }) => {
  let cancels = 0
  await page.route('**/api/v1/etf-shares/collection-status', (r) =>
    r.fulfill({
      json: {
        ...ready,
        status: 'running',
        manual: { id: 'old', status: 'complete' },
        progress: { phase: 'fetching', completed: 1, total: 10 },
      },
    }),
  )
  await page.route('**/api/v1/etf-shares/collection-cancel', (r) => {
    cancels++
    return r.abort()
  })
  await page.goto('/etf-shares')
  await expect(page.getByRole('button', { name: '取消采集', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '自动采集中…', exact: true })).toBeDisabled()
  expect(cancels).toBe(0)
})

test('uncertain cancellation retries only the current manual task', async ({ page }) => {
  const ids: string[] = []
  let starts = 0
  let manual = { id: 'existing-job', status: 'running' }
  await page.route('**/api/v1/etf-shares/collection-status', (r) =>
    r.fulfill({ json: { ...ready, manual } }),
  )
  await page.route('**/api/v1/etf-shares/collection-run', (r) => {
    starts++
    return r.abort()
  })
  await page.route('**/api/v1/etf-shares/collection-cancel', (r) => {
    ids.push(r.request().postDataJSON().request_id)
    if (ids.length === 1) return r.abort()
    manual = { ...manual, status: 'cancelled' }
    return r.fulfill({ json: manual })
  })
  await page.goto('/etf-shares')
  await page.getByRole('button', { name: '取消采集', exact: true }).click()
  await expect(page.getByText(/取消结果待确认/)).toBeVisible()
  await page.getByRole('button', { name: '取消采集', exact: true }).click()
  await expect(page.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
  expect(ids).toEqual(['existing-job', 'existing-job'])
  expect(starts).toBe(0)
})
