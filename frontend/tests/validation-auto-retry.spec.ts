import { expect, test } from '@playwright/test'

function waiting() {
  return {
    id: 'retry-task',
    methods: ['oversold'],
    start: '2024-01-01',
    end: '2026-09-30',
    observed: '2026-09-30',
    version: 'four-close-v2',
    input_mode: 'static-batch-v1',
    status: 'retry_wait',
    stage: '等待自动重试',
    done: 7,
    total: 20,
    captured: 0,
    auto_retry_count: 2,
    next_retry_at: '2026-10-31T01:01:00Z' as string | null,
    retry_exhausted: false,
    error: '源数据库连接中断；已提交断点保留，将自动重试。' as string | null,
    summary: [],
    windows: [],
  }
}

test('waiting retries persist across refresh; browser only polls and manual retry is explicit', async ({
  page,
}, info) => {
  let job = waiting()
  const posts: string[] = []
  await page.clock.install({ time: new Date('2026-10-31T01:00:00Z') })
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
    if (r.request().method() === 'POST') {
      posts.push(path)
      job = {
        ...job,
        status: 'queued',
        stage: '等待从已提交断点继续',
        auto_retry_count: 0,
        next_retry_at: null,
        error: null,
      }
      return r.fulfill({ json: job })
    }
    return r.fulfill({ json: { jobs: [job] } })
  })
  await page.goto('/short-term/validation')
  const card = page.locator('.validation-job')
  await expect(card).toContainText('自动重试：2/5')
  await expect(card).toContainText('下次')
  await expect(card.getByRole('progressbar', { name: '已提交股票' })).toHaveAttribute('value', '7')
  await expect(card.getByRole('button', { name: '暂停任务' })).toBeEnabled()
  await expect(card.getByRole('button', { name: '取消任务' })).toBeEnabled()
  await page.reload()
  await expect(card).toContainText('自动重试：2/5')
  await page.clock.runFor(61000)
  expect(posts).toEqual([]) // The server, never the browser, schedules retries.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: info.outputPath('auto-retry-waiting.png'),
    fullPage: true,
    animations: 'disabled',
  })
  await card.getByRole('button', { name: '重试', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(card).toHaveAttribute('data-status', 'queued')
  expect(posts).toEqual(['/api/v1/short-term/validation/jobs/retry-task/retry'])
  await expect(card).not.toContainText('下次')
  await expect(card).not.toContainText('自动重试：')
})

test('waiting can pause, cancel or delete and exhausted budget is explicit', async ({
  page,
}, info) => {
  let job: ReturnType<typeof waiting> | null = waiting()
  const posts: string[] = []
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
    if (r.request().method() === 'DELETE') {
      job = null
      return r.fulfill({ json: { deleted: true } })
    }
    if (r.request().method() === 'POST' && job) {
      posts.push(path)
      job = {
        ...job,
        status: path.endsWith('/pause') ? 'paused' : 'cancelled',
        next_retry_at: null,
      }
      return r.fulfill({ json: job })
    }
    return r.fulfill({ json: { jobs: job ? [job] : [] } })
  })
  await page.goto('/short-term/validation')
  const card = page.locator('.validation-job')
  await card.getByRole('button', { name: '暂停任务' }).click()
  await expect(card).toHaveAttribute('data-status', 'paused')
  await expect(card).not.toContainText('下次')
  job = waiting()
  await page.reload()
  await card.getByRole('button', { name: '取消任务' }).click()
  await expect(card).toHaveAttribute('data-status', 'cancelled')
  await expect(card.getByRole('button', { name: '重试', exact: true })).toHaveCount(0)
  job = {
    ...waiting(),
    status: 'failed',
    stage: '自动重试已用尽',
    next_retry_at: null,
    auto_retry_count: 5,
    retry_exhausted: true,
    error: '源数据库连接中断；自动重试5次已用尽，请检查后手动重试。',
  }
  await page.reload()
  await expect(card).toContainText('自动重试：5/5')
  await expect(card).toContainText('次数已用尽')
  await expect(card.getByRole('button', { name: '重试', exact: true })).toBeEnabled()
  await expect(card.getByRole('button', { name: '暂停任务' })).toHaveCount(0)
  await page.screenshot({
    path: info.outputPath('auto-retry-exhausted.png'),
    fullPage: true,
    animations: 'disabled',
  })
  job = { ...waiting(), status: 'completed', error: null, next_retry_at: null }
  await page.reload()
  await expect(card).toHaveAttribute('data-status', 'completed')
  await expect(card).not.toContainText('自动重试：')
  expect(posts).toHaveLength(2)
  job = waiting()
  await page.reload()
  await card.getByRole('button', { name: '删除记录' }).click()
  const dialog = page.getByRole('dialog', { name: '删除这条验证记录？' })
  await dialog.getByRole('button', { name: '保留记录' }).click()
  await expect(card).toBeVisible()
  await card.getByRole('button', { name: '删除记录' }).click()
  await dialog.getByRole('button', { name: '永久删除记录和数据' }).click()
  await expect(card).toHaveCount(0)
  await page.reload()
  await expect(card).toHaveCount(0)
})
