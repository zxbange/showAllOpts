import { expect, test, adminSession } from './fixtures'
import type { ValuationHistory, ValuationStatus } from '../src/types/valuationRuler'
const point = {
  date: '2026-11-10',
  index_close: 4100,
  mean_pe_ttm: 24.5,
  member_count: 300,
  positive_count: 280,
  loss_count: 20,
  source: 'joinquant',
}
const history: ValuationHistory = {
  points: [point],
  latest: point,
  method: 'csi300_equal_positive_pe_ttm_v1',
  benchmarks: {
    p20: 17.5,
    p80: 32.2,
    sample_days: 1,
    start_date: point.date,
    end_date: point.date,
  },
  coverage: {
    requested_start: '2010-01-01',
    first_date: point.date,
    last_date: point.date,
    missing_count: null,
    known_missing_count: 2,
    calendar_complete: false,
  },
  gap_dates: ['2026-11-06', '2026-11-09'],
  sources: ['joinquant'],
}
const idle: ValuationStatus = {
  status: 'idle',
  phase: null,
  target_date: point.date,
  updated_at: null,
  error: null,
  completed_days: 1,
  failed_days: 0,
  schedule: '20:00 Asia/Shanghai',
  source_configured: true,
  automatic: false,
  recent_failures: [],
  cancelled: false,
  comparison_status: 'not_compared',
  comparison: null,
  comparison_error: null,
  comparison_attempted_date: null,
  joinquant_imported: null,
  joinquant_conflicts: 0,
  last_success_at: null,
}

test('valuation tab, ranges, real quantiles and disclosure render without overflow', async ({
  page,
}) => {
  await page.route('**/api/v1/valuation-ruler/**', (route) =>
    route.fulfill({ json: route.request().url().endsWith('/status') ? idle : history }),
  )
  await page.goto('/valuation-ruler')
  await expect(page.getByRole('heading', { name: '市场情绪 估值标尺' })).toBeVisible()
  await expect(
    page
      .getByRole('navigation', { name: '市场情绪模块切换' })
      .getByRole('link', { name: '估值标尺' }),
  ).toHaveAttribute('aria-current', 'page')
  await expect(page.getByText('17.50', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '近1年', exact: true }).click()
  await expect(page.getByText('17.50', { exact: true })).toBeVisible()
  await expect(page.getByText('尚未完成重叠样本对照，不能宣称来源一致。')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('running prevents duplicate submissions and allows cancellation', async ({ page }) => {
  let status = idle
  let starts = 0
  await page.route('**/api/v1/valuation-ruler/**', async (route) => {
    const path = new URL(route.request().url()).pathname.split('/').at(-1)
    if (path === 'refresh') {
      starts++
      status = { ...idle, status: 'running' }
      await route.fulfill({ status: 202, json: { started: true } })
    } else if (path === 'cancel') {
      status = { ...idle, cancelled: true }
      await route.fulfill({ status: 202, json: { cancelled: true } })
    } else await route.fulfill({ json: path === 'status' ? status : history })
  })
  await page.goto('/valuation-ruler')
  await page.getByRole('button', { name: '采集并补数据', exact: true }).click()
  await expect(page.getByRole('button', { name: '采集中', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '同步聚宽历史', exact: true })).toBeDisabled()
  expect(starts).toBe(1)
  await page.getByRole('button', { name: '停止采集', exact: true }).click()
  await expect(page.getByRole('button', { name: '采集并补数据', exact: true })).toBeEnabled()
})

test('failed uncertain start remains blocked after navigation and fresh idle status', async ({
  page,
}) => {
  let starts = 0
  await page.route('**/api/v1/valuation-ruler/**', async (route) => {
    if (route.request().method() === 'POST') {
      starts++
      await route.abort('failed')
    } else await route.fulfill({ json: route.request().url().endsWith('/status') ? idle : history })
  })
  await page.goto('/valuation-ruler')
  await page.getByRole('button', { name: '采集并补数据', exact: true }).click()
  await expect(page.getByText(/上次操作结果不确定/)).toBeVisible()
  await page.getByRole('button', { name: '重新读取采集状态' }).click()
  await expect(page.getByRole('button', { name: '采集并补数据', exact: true })).toBeDisabled()
  await page.getByRole('link', { name: '用户管理', exact: true }).click()
  await page.goBack()
  await expect(page.getByRole('button', { name: '采集并补数据', exact: true })).toBeDisabled()
  expect(starts).toBe(1)
})

test('viewer cannot mutate and complete JQ day comparison failure is separate from gaps', async ({
  page,
}) => {
  await page.route('**/api/auth/**', (route) =>
    route.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } } }),
  )
  await page.route('**/api/v1/valuation-ruler/**', (route) =>
    route.fulfill({
      json: route.request().url().endsWith('/status')
        ? {
            ...idle,
            comparison_error: '中证最新成分文件过期',
            comparison_attempted_date: point.date,
            recent_failures: [
              {
                trade_date: '2026-11-09',
                code: 'zero_pe',
                reason: 'PE为零，不能作为亏损剔除',
                attempts: 2,
                updated_at: point.date,
              },
            ],
          }
        : history,
    }),
  )
  await page.goto('/valuation-ruler')
  await expect(page.getByRole('button', { name: '采集并补数据', exact: true })).toBeDisabled()
  await expect(page.getByText(/对照失败不等于该日缺失/)).toBeVisible()
  await expect(page.getByText(/PE为零，不能作为亏损剔除/)).toBeVisible()
  await expect(page.getByText('24.50', { exact: true })).toBeVisible()
})

test('older status cannot re-enable collection after a newer running response', async ({
  page,
}) => {
  let status = idle
  let release: (() => void) | undefined
  let hold = false
  let held = false
  await page.route('**/api/v1/valuation-ruler/**', async (route) => {
    const path = new URL(route.request().url()).pathname.split('/').at(-1)
    if (path === 'status') {
      const captured = status
      if (hold && !held) {
        held = true
        await new Promise<void>((resolve) => {
          release = resolve
        })
      }
      await route.fulfill({ json: captured }).catch(() => {})
    } else if (path === 'refresh') {
      status = { ...idle, status: 'running' }
      await route.fulfill({ status: 202, json: { started: true } })
    } else await route.fulfill({ json: history })
  })
  await page.goto('/valuation-ruler')
  await expect(page.getByRole('button', { name: '采集并补数据', exact: true })).toBeEnabled()
  hold = true
  await page.getByRole('button', { name: '重新读取采集状态' }).click()
  await expect.poll(() => held).toBe(true)
  await page.getByRole('button', { name: '采集并补数据', exact: true }).click()
  await expect(page.getByRole('button', { name: '采集中', exact: true })).toBeDisabled()
  release?.()
  await expect(page.getByRole('button', { name: '采集中', exact: true })).toBeDisabled()
})

test('pending POST remains locked across route departure and return', async ({ page }) => {
  let release: (() => void) | undefined
  let starts = 0
  await page.route('**/api/v1/valuation-ruler/**', async (route) => {
    if (route.request().method() === 'POST') {
      starts++
      await new Promise<void>((resolve) => {
        release = resolve
      })
      await route.fulfill({ status: 202, json: { started: true } })
    } else await route.fulfill({ json: route.request().url().endsWith('/status') ? idle : history })
  })
  await page.goto('/valuation-ruler')
  await page.getByRole('button', { name: '采集并补数据', exact: true }).click()
  await expect.poll(() => starts).toBe(1)
  await page.getByRole('link', { name: '用户管理', exact: true }).click()
  await page.goBack()
  await expect(page.getByRole('button', { name: '正在提交…', exact: true })).toBeDisabled()
  expect(starts).toBe(1)
  release?.()
})
