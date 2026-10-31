import { expect, test } from '@playwright/test'

test('execution time and task details across lifecycle', async ({ page }, info) => {
  let job: Record<string, unknown> = {
    id: 'timing',
    methods: ['oversold'],
    start: '2024-01-01',
    end: '2026-10-26',
    observed: '2026-10-30',
    version: 'four-close-v1',
    input_mode: 'static-batch-v1',
    status: 'queued',
    stage: '等待',
    done: 3,
    total: 20,
    captured: 0,
    execution_seconds: 0,
    execution_partial: false,
    first_started_at: null,
    stage_started_at: '2026-10-31T10:00:00Z',
    timings: { connect: 0.5 },
    source_name: '研究库',
    summary: [],
    windows: [],
  }
  await page.clock.install()
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], dates: [], counts: {} } }),
  )
  await page.route('**/api/v1/short-term/validation/**', (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({
        json: { mode: 'pair', authenticated: true, csrf_token: 'mock', https_required: false },
      })
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [] } })
    return r.fulfill({ json: { jobs: [job] } })
  })
  await page.goto('/short-term/validation')
  const card = page.locator('.validation-job')
  const time = card.locator('.validation-execution-time')
  await expect(time).toContainText('00:00:00')
  await card.getByText('任务参数', { exact: true }).click()
  const meta = card.locator('.validation-task-meta')
  await expect(meta).toContainText('任务开始：尚未开始')
  await expect(meta).toContainText('最近阶段耗时（秒，非累计）')
  await expect(meta).toContainText('连接 0.500')
  await expect(meta).toContainText('数据源：研究库')
  await expect(meta).not.toContainText('已提交')
  await expect(card).not.toContainText('旧版口径')
  await expect(card).not.toContainText('阶段开始')
  await expect(card).not.toContainText('删除前请先取消任务')
  job = {
    ...job,
    status: 'running',
    stage: '计算',
    first_started_at: '2026-10-31T01:00:00Z',
    execution_seconds: 3661,
  }
  await page.clock.runFor(3500)
  await expect(time).toContainText('01:01:01')
  const started = await meta.locator('p').first().textContent()
  await expect(card.getByRole('button', { name: '删除记录' })).toBeDisabled()
  await page.screenshot({ path: info.outputPath('running-timing.png'), fullPage: true })
  for (const status of ['paused', 'retry_wait', 'failed', 'cancelling', 'cancelled', 'completed']) {
    job = { ...job, status, stage: status }
    await page.clock.runFor(3500)
    await expect(card).toHaveAttribute('data-status', status)
    await expect(time).toContainText('01:01:01')
    await expect(meta.locator('p').first()).toHaveText(started!)
  }
  await page.screenshot({ path: info.outputPath('completed-timing.png'), fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  job = { ...job, status: 'queued' }
  await page.reload()
  await card.getByText('任务参数', { exact: true }).click()
  await expect(time).toContainText('01:01:01')
  await expect(meta.locator('p').first()).toHaveText(started!)
  job = { ...job, status: 'completed', execution_token: 'unfinished-timing-save' }
  await page.reload()
  await expect(time).toContainText('约 01:01:01')
  delete job.execution_token
  // Legacy records must not infer start/duration from their stage timestamps.
  delete job.execution_seconds
  delete job.execution_partial
  delete job.first_started_at
  await page.reload()
  await card.getByText('任务参数', { exact: true }).click()
  await expect(meta).toContainText('任务开始：未知（旧任务）')
  await expect(time).toContainText('未知')
  job = { ...job, execution_seconds: 25, execution_partial: true }
  await page.reload()
  await expect(time).toContainText('已记录 00:00:25')
})
