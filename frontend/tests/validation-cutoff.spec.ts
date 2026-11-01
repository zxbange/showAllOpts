import { expect, test, type Page } from './fixtures'
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

test('custom periods validate boundaries, submit and render only requested results', async ({
  page,
}) => {
  await mock(page)
  const startButton = page.getByRole('button', { name: '开始验证', exact: true })
  await expect(page.getByText('请输入数据库密码。', { exact: true })).toHaveCount(0)
  await expect(page.getByText('后台加密保存', { exact: true })).toHaveCount(0)
  await expect(page.getByText('统计口径与数据说明', { exact: true })).toHaveCount(0)
  await expect(page.getByLabel('数据库TLS')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '添加周期' })).toBeDisabled()
  for (const invalid of ['0', '121', '1.5', '']) {
    await page.getByLabel('验证周期1', { exact: true }).fill(invalid)
    await expect(startButton).toBeDisabled()
  }
  await page.getByLabel('验证周期1', { exact: true }).fill('2')
  await expect(startButton).toBeDisabled() // Duplicate second period.
  await page.getByLabel('验证周期1', { exact: true }).fill('120')
  await page.getByRole('button', { name: '删除周期3' }).click()
  await page.getByRole('button', { name: '删除周期2' }).click()
  await expect(page.getByRole('button', { name: '删除周期1' })).toBeDisabled()
  await page.getByRole('button', { name: '添加周期' }).click()
  await expect(startButton).toBeDisabled()
  await page.getByLabel('验证周期2', { exact: true }).fill('7')
  await expect(startButton).toBeEnabled()
  let payload: Record<string, unknown> | undefined
  const completed = {
    ...job('completed'),
    horizons: [7, 120],
    windows: [],
    summary: [7, 120].map((day) => ({
      策略: 'bukou',
      后续交易日: day,
      有效样本: 100,
      '胜率(%)': day === 7 ? 61 : 72,
      '平均收益(%)': 5,
      '中位收益(%)': 4,
    })),
  }
  await page.route('**/validation/jobs', async (route) => {
    if (route.request().method() !== 'POST')
      return route.fulfill({ json: { jobs: payload ? [completed] : [] } })
    payload = route.request().postDataJSON()
    return route.fulfill({ json: completed })
  })
  await startButton.click()
  await expect.poll(() => payload?.horizons).toEqual([120, 7])
  await expect(page.getByRole('button', { name: 'T+7', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'T+120', exact: true }).click()
  await expect(page.getByRole('region', { name: 'T+120策略对比' })).toContainText('72')
  await expect(page.getByRole('button', { name: 'T+1', exact: true })).toHaveCount(0)
  await expect(page.getByText('三日窗口统计', { exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('button', { name: 'T+120', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
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
  await expect(page.locator('.validation-strategies > label')).toHaveCount(7)
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
    methods: ['bukou', 'oversold'],
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
