import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
const job = (status = 'failed', version = 'four-close-v2') => ({
  id: 'cutoff-job',
  methods: ['bukou'],
  start: '2024-01-01',
  end: '2026-09-30',
  observed: '2026-10-09',
  version,
  status,
  stage: '测试任务',
  captured: 12,
  done: 10,
  total: 12,
  error: null,
  summary: [],
  windows: [],
  input_mode: 'static-batch-v1',
})
async function mock(page: Page, jobs: ReturnType<typeof job>[] = []) {
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
    if (path.endsWith('/sources/save'))
      return r.fulfill({ json: { id: 'a'.repeat(32), revision: 1, ...r.request().postDataJSON() } })
    return r.fulfill({ json: { jobs } })
  })
  await page.goto('/short-term/validation')
}
async function screenshot(page: Page, name: string) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  if (!process.env.VALIDATION_SCREENSHOTS) return
  await mkdir(process.env.VALIDATION_SCREENSHOTS, { recursive: true })
  await page.screenshot({
    path: `${process.env.VALIDATION_SCREENSHOTS}/${name}.png`,
    fullPage: true,
    animations: 'disabled',
  })
}
test('two dates submit one cutoff and cards adapt to extra strategies', async ({ page }, info) => {
  await mock(page)
  await expect(page.locator('input[type=date]')).toHaveCount(2)
  await expect(page.getByLabel('验证开始日', { exact: true })).toBeVisible()
  await expect(page.getByLabel('验证截止日', { exact: true })).toBeVisible()
  await expect(page.getByText(/截止日期留空/)).toHaveCount(0)
  const cards = page.locator('.validation-setup-grid > .validation-card')
  async function layout() {
    // Read both cards in the same animation frame, not separate browser calls.
    const [a, b] = await cards.evaluateAll((elements) =>
      elements.map((element) => {
        const rect = element.getBoundingClientRect()
        return { y: rect.y, height: rect.height }
      }),
    )
    if (info.project.name === 'desktop') {
      expect(Math.abs(a.height - b.height)).toBeLessThan(1)
      expect(Math.abs(a.y - b.y)).toBeLessThan(1)
    } else {
      expect(b.y).toBeGreaterThanOrEqual(a.y + a.height)
      expect(b.y - a.y - a.height).toBeLessThan(24)
      const gap = await cards
        .nth(1)
        .evaluate(
          (el) =>
            el.getBoundingClientRect().bottom -
            el.querySelector('.validation-start')!.getBoundingClientRect().bottom,
        )
      expect(gap).toBeLessThan(45)
    }
  }
  await layout()
  await screenshot(page, `${info.project.name}-cutoff-configuration`)
  await page.locator('.validation-strategies').evaluate((el) => {
    for (let i = 0; i < 5; i++) {
      const label = el.querySelector('label')!.cloneNode(true) as HTMLElement
      label.querySelector('span')!.textContent = `模拟扩展策略 ${i + 1} 较长名称`
      label.querySelector('input')!.setAttribute('aria-label', `模拟策略${i + 1}`)
      el.append(label)
    }
  })
  await expect(page.locator('.validation-strategies > label')).toHaveCount(9)
  await layout()
  expect(
    await page
      .locator('.validation-strategies > label')
      .evaluateAll((els) => new Set(els.map((el) => el.getBoundingClientRect().top)).size),
  ).toBeGreaterThan(2)
  await screenshot(page, `${info.project.name}-cutoff-expanded-strategies`)
  await page.getByLabel('验证开始日', { exact: true }).fill('2026-09-01')
  await page.getByLabel('验证截止日', { exact: true }).fill('2026-09-30')
  let payload: Record<string, unknown> | undefined
  await page.route('**/validation/jobs', async (r) => {
    if (r.request().method() !== 'POST') return r.fulfill({ json: { jobs: [] } })
    payload = r.request().postDataJSON()
    await r.fulfill({ json: job('queued') })
  })
  await page.getByRole('button', { name: '开始验证', exact: true }).click()
  await expect.poll(() => payload).toBeTruthy()
  expect(payload).toMatchObject({
    start: '2026-09-01',
    end: '2026-09-30',
    methods: ['bukou', 'redhover', 'xuli', 'oversold'],
  })
  expect(payload).not.toHaveProperty('observed')
  await expect(page.getByText('信号与收益均截至验证截止日', { exact: false })).toBeVisible()
})
test('retry matches adjacent controls and preserves busy guard and keyboard interaction', async ({
  page,
}, info) => {
  await mock(page, [job()])
  const retry = page.getByRole('button', { name: '重试', exact: true })
  await expect(retry).toBeVisible()
  const styles = await page.locator('.validation-job-actions button').evaluateAll((els) =>
    els.map((el) => {
      const s = getComputedStyle(el)
      return {
        height: el.getBoundingClientRect().height,
        minWidth: s.minWidth,
        padding: s.padding,
        background: s.backgroundColor,
        color: s.color,
      }
    }),
  )
  expect(styles).toHaveLength(3)
  for (const s of styles.slice(1)) {
    expect(s.height).toBe(styles[0].height)
    expect(s.minWidth).toBe(styles[0].minWidth)
    expect(s.padding).toBe(styles[0].padding)
  }
  expect(styles[0].background).toBe('rgb(255, 255, 255)')
  expect(styles[0].color).toBe('rgb(33, 123, 112)')
  await retry.focus()
  await expect(retry).toBeFocused()
  await expect(retry).toHaveCSS('outline-style', 'solid')
  await expect(retry).toHaveCSS('outline-color', 'rgb(33, 123, 112)')
  if (info.project.name === 'desktop') {
    await retry.hover()
    await expect(retry).toHaveCSS('background-color', 'rgb(240, 250, 246)')
  }
  await screenshot(page, `${info.project.name}-cutoff-retry`)
  let release!: () => void
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  let calls = 0
  await page.route('**/validation/jobs/cutoff-job/retry', async (r) => {
    calls++
    expect(r.request().method()).toBe('POST')
    expect(r.request().postDataJSON()).toEqual({})
    await held
    await r.fulfill({ json: job('running') })
  })
  await retry.focus()
  await page.keyboard.press('Enter')
  await expect(retry).toBeDisabled()
  await expect(retry).toHaveCSS('opacity', '0.55')
  await expect(retry).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  await page.keyboard.press('Enter')
  expect(calls).toBe(1)
  release()
  await expect(retry).toHaveCount(0)
})
for (const status of ['paused', 'queued', 'running', 'cancelling', 'cancelled', 'completed']) {
  test(`retry eligibility remains ${status}`, async ({ page }) => {
    await mock(page, [job(status, 'four-close-v1')])
    await expect(page.getByText('旧版口径：观察截至 2026-10-09', { exact: false })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '重试', exact: true })).toHaveCount(
      status === 'paused' ? 1 : 0,
    )
  })
}
