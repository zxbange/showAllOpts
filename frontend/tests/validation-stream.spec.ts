import { expect, test } from '@playwright/test'

test('static task shows committed progress and timings; legacy freeze remains distinguishable', async ({
  page,
}) => {
  const base = {
    methods: ['bukou'],
    start: '2024-01-01',
    end: '2025-01-01',
    observed: '2025-01-07',
    status: 'running',
    done: 20,
    total: 23,
    captured: 0,
    updated_at: '2026-10-30T00:00:00Z',
    version: 'four-close-v1',
    summary: [],
    windows: [],
  }
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { enabled: false, modules: [] } }))
  await page.route('**/api/v1/short-term/validation/**', (r) => {
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/access'))
      return r.fulfill({
        json: { mode: 'pair', authenticated: true, csrf_token: 'test', https_required: false },
      })
    if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [] } })
    return r.fulfill({
      json: {
        jobs: [
          {
            ...base,
            id: 'static',
            input_mode: 'static-batch-v1',
            stage: '读取批次 21–23 / 23',
            timings: {
              connect: 0.03,
              ultra_research_bar: 0.4,
              ultra_research_universe: 0.2,
              compute: 1.4,
              commit: 0.01,
            },
          },
          {
            ...base,
            id: 'old',
            status: 'paused',
            done: 0,
            captured: 67,
            total: 5133,
            stage: '已暂停',
          },
        ],
      },
    })
  })
  await page.goto('/short-term/validation')
  const tasks = page.locator('.validation-job')
  await expect(tasks).toHaveCount(2)
  await expect(tasks.first().getByRole('progressbar', { name: '已提交股票' })).toHaveAttribute(
    'value',
    '20',
  )
  await expect(tasks.first().getByRole('status')).toContainText(
    '读取批次 21–23 / 23 · 已提交 20/23',
  )
  await expect(tasks.first().getByRole('progressbar', { name: '冻结输入' })).toHaveCount(0)
  await tasks.first().locator('summary').filter({ hasText: '任务参数' }).click()
  await expect(tasks.first()).toContainText('静态源分批读取')
  await expect(tasks.first()).toContainText('最近阶段耗时（秒，非累计）')
  await expect(tasks.first()).toContainText('行情读取 0.400')
  await expect(tasks.last().getByRole('progressbar', { name: '冻结输入' })).toHaveAttribute(
    'value',
    '67',
  )
  await tasks.last().locator('summary').filter({ hasText: '任务参数' }).click()
  await expect(tasks.last()).toContainText('旧模式：先冻结输入，再计算')
  await page.locator('summary').filter({ hasText: '配置新的验证任务' }).click()
  await expect(page.getByText('新任务每批最多20股', { exact: false })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
