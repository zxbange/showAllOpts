import { expect, test, type Page } from '@playwright/test'

async function unlock(page: Page) {
  await page.getByLabel('验证管理密钥').fill('mock-key')
  await page.getByRole('button', { name: '解锁验证管理' }).click()
}
async function openSetup(page: Page) {
  const setup = page.locator('.validation-setup')
  if (!(await setup.getAttribute('open'))) {
    // An open boolean attribute has an empty string value.
    if (!(await setup.evaluate((e) => (e as HTMLDetailsElement).open)))
      await setup.locator('summary').first().click()
  }
}

test('start rotates accepted intent, deduplicates active requests, and keeps terminal history', async ({
  page,
}) => {
  const requests: string[] = []
  const byRequest = new Map<string, any>()
  const jobs: any[] = []
  let state = 'completed'
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], jobs, sources: [], https_required: false } }),
  )
  await page.route('**/validation/jobs', (r) => {
    if (r.request().method() === 'GET') return r.fulfill({ json: { jobs } })
    const body = r.request().postDataJSON()
    requests.push(body.request_id)
    let job =
      byRequest.get(body.request_id) || jobs.find((j) => ['queued', 'running'].includes(j.status))
    if (!job) {
      job = {
        ...body,
        id: String(jobs.length + 1),
        status: state,
        stage: state,
        done: 0,
        captured: 0,
        total: 1,
        summary: [],
        windows: [],
      }
      jobs.unshift(job)
    }
    byRequest.set(body.request_id, job)
    return r.fulfill({ json: job })
  })
  await page.goto('/short-term/validation')
  await unlock(page)
  await expect(page.getByRole('button', { name: '准备重新读取数据' })).toHaveCount(0)
  for (const status of ['completed', 'failed', 'cancelled', 'running', 'running']) {
    state = status
    await openSetup(page)
    const previous = requests.length
    await page.getByRole('button', { name: '开始验证', exact: true }).click()
    await expect.poll(() => requests.length).toBe(previous + 1)
    await expect
      .poll(() => page.evaluate(() => sessionStorage.getItem('validation-pending-start')))
      .toBeNull()
  }
  expect(new Set(requests).size).toBe(5)
  expect(jobs.length).toBe(4)
  await expect(page.locator('.validation-job')).toHaveCount(4)
})

test('uncertain response and refresh retain intent; double clicks submit once', async ({
  page,
}) => {
  const ids: string[] = []
  let release: (() => void) | undefined
  let loseResponse = true
  const jobs: any[] = []
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({ json: { enabled: false, modules: [], jobs, sources: [], https_required: false } }),
  )
  await page.route('**/validation/jobs', async (r) => {
    if (r.request().method() === 'GET') return r.fulfill({ json: { jobs } })
    const body = r.request().postDataJSON()
    ids.push(body.request_id)
    if (!jobs.length)
      jobs.push({
        ...body,
        id: 'existing',
        status: 'completed',
        stage: '完成',
        done: 0,
        captured: 0,
        total: 1,
        summary: [],
        windows: [],
      })
    if (loseResponse) {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return r.abort('failed')
    }
    return r.fulfill({ json: jobs[0] })
  })
  await page.goto('/short-term/validation')
  await unlock(page)
  const start = page.getByRole('button', { name: '开始验证', exact: true })
  await start.evaluate((button: HTMLButtonElement) => {
    button.click()
    button.click()
  })
  await expect.poll(() => ids.length).toBe(1)
  await expect(start).toBeDisabled()
  release!()
  await expect(start).toBeEnabled()
  expect(await page.evaluate(() => sessionStorage.getItem('validation-pending-start'))).toBe(ids[0])
  await page.reload()
  await unlock(page)
  await openSetup(page)
  loseResponse = false
  await page.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect.poll(() => ids.length).toBe(2)
  expect(ids[1]).toBe(ids[0])
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem('validation-pending-start')))
    .toBeNull()
  expect(jobs.length).toBe(1)
})
