import { readFileSync } from 'node:fs'
import { test, expect, adminSession } from './fixtures'
const seed = JSON.parse(readFileSync('../backend/app/data/cci_coal.json', 'utf8'))
const data = {
  rows: seed.rows,
  missingChanges: 238,
  status: { running: false, stage: '采集完成', checkedAt: '2026-11-03T01:27:20Z' },
}

test('CCI peer module, gaps, grade selection, schedule, responsive navigation', async ({
  page,
}, info) => {
  let calls = 0
  await page.route('**/api/v1/commodities/cci**', async (route) => {
    if (route.request().method() === 'POST') {
      calls++
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      return route.fulfill({ json: { started: true } })
    }
    return route.fulfill({ json: data })
  })
  await page.route('**/api/v1/commodities/a00', (route) =>
    route.fulfill({ json: { rows: [], status: { running: false, automatic: true } } }),
  )
  await page.goto('/commodities?instrument=cci')
  await expect(page.getByRole('heading', { name: 'CCI动力煤', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'CCI动力煤', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.locator('.commodity-chart canvas')).toBeVisible()
  await expect(page.getByText(/每天 21:00 自动采集/)).toBeVisible()
  await expect(page.getByText('三档人民币报价 · 元/吨 · 自2026-01-01')).toHaveCount(0)
  await expect(page.locator('.commodity-source-note')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'coalindex.com 网络汇集（非官方）' })).toHaveCount(0)
  await expect(page.getByText(/每30分钟重试，最多3次，跨日停止/)).toHaveCount(0)
  await expect(page.getByText(/最近采集：/)).toHaveCount(0)
  await expect(page.getByText(/11:00/)).toHaveCount(0)
  await expect(page.getByRole('row')).toHaveCount(587)
  await page.getByLabel('CCI报价档位').selectOption('CCI5000')
  await expect(page.getByRole('row')).toHaveCount(196)
  await expect(page.getByRole('cell', { name: '2026-01-21', exact: true })).toHaveCount(0)
  await expect(page.getByRole('cell', { name: '—', exact: true }).first()).toBeVisible()
  await page.getByLabel('CCI报价档位').selectOption('all')
  await page.getByRole('button', { name: '更新数据' }).click()
  expect(calls).toBe(1)
  await expect(page.getByRole('button', { name: '更新数据' })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    window.scrollTo(0, 0)
  })
  await page.screenshot({ path: info.outputPath('cci.png'), fullPage: true })
  await page.getByRole('link', { name: 'A00铝', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'A00铝', exact: true })).toBeVisible()
  await expect(page.getByText(/每天 11:00、18:00/)).toBeVisible()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'CCI动力煤', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'CCI动力煤', exact: true })).toBeVisible()
})

test('CCI failed refresh retains displayed history and prevents repeated start', async ({
  page,
}) => {
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  let posts = 0
  await page.route('**/api/v1/commodities/cci**', async (route) => {
    if (route.request().method() === 'POST') {
      posts++
      await pending
      return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
    }
    return route.fulfill({ json: data })
  })
  await page.goto('/commodities?instrument=cci')
  await page.getByRole('button', { name: '更新数据' }).click()
  await expect(page.getByRole('button', { name: '更新中…' })).toBeDisabled()
  expect(posts).toBe(1)
  release()
  await expect(page.getByRole('alert')).toContainText('CCI更新未启动')
  await expect(page.getByRole('row')).toHaveCount(587)
})

for (const outcome of ['empty', 'partial', 'failed', 'completed']) {
  test(`CCI today status ${outcome} preserves history`, async ({ page }) => {
    const hits = outcome === 'completed' ? 3 : outcome === 'partial' ? 1 : 0
    const stage =
      outcome === 'empty'
        ? '来源无当日报价，历史保留'
        : outcome === 'failed'
          ? '当日报价读取失败，历史保留'
          : outcome === 'partial'
            ? '当日报价部分可用'
            : '当日三档报价已核验'
    await page.route('**/api/v1/commodities/cci', (route) =>
      route.fulfill({
        json: {
          ...data,
          status: {
            ...data.status,
            stage,
            targetDate: '2026-11-03',
            observed: hits,
            targetHits: hits,
            inserted: hits,
            changed: 0,
            grades: ['CCI5500', 'CCI5000', 'CCI4500'].map((grade, index) => ({
              grade,
              returned: index < hits ? 1 : 0,
              latestQuoteDate: index < hits ? '2026-11-03' : null,
              targetHits: index < hits ? 1 : 0,
              error: outcome === 'failed' ? '网络请求失败' : null,
            })),
          },
        },
      }),
    )
    await page.goto('/commodities?instrument=cci')
    await expect(page.getByText(stage, { exact: false })).toHaveCount(0)
    const status = page.getByRole('status', { name: 'CCI数据更新状态' })
    await expect(status).toContainText(`命中 ${hits}/3 档`)
    await expect(status).toContainText('今日（2026-11-03）')
    await expect(page.getByRole('row')).toHaveCount(587)
    await expect(page.getByRole('button', { name: '更新数据' })).toBeEnabled()
    await expect(page.getByText(/不做更早的全量历史补采/)).toHaveCount(0)
    await expect(page.getByText(/手动与自动更新均按三档分别补齐/)).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}

const recoveryStatus = {
  running: false,
  targetDate: '2026-11-09',
  observed: 15,
  targetHits: 2,
  inserted: 12,
  changed: 1,
  recovery: {
    dateFrom: '2026-11-04',
    dateTo: '2026-11-09',
    initializedFrom: [{ grade: 'CCI5500', dateFrom: '2026-11-04', origin: 'latest_quote' }],
    pendingCount: 5,
    checkedCount: 15,
    unavailableCount: 2,
    deferredCount: 3,
    requestCount: 4,
  },
  grades: [
    {
      grade: 'CCI5500',
      dateFrom: '2026-11-09',
      dateTo: '2026-11-09',
      returned: 1,
      targetHits: 1,
      latestQuoteDate: '2026-11-09',
      error: null,
    },
    {
      grade: 'CCI5500',
      dateFrom: '2026-11-04',
      dateTo: '2026-11-08',
      returned: 5,
      targetHits: 0,
      latestQuoteDate: '2026-11-08',
      error: null,
    },
    {
      grade: 'CCI5000',
      dateFrom: '2026-11-04',
      dateTo: '2026-11-09',
      returned: 9,
      targetHits: 1,
      latestQuoteDate: '2026-11-09',
      error: null,
    },
    {
      grade: 'CCI4500',
      dateFrom: '2026-11-09',
      dateTo: '2026-11-09',
      returned: 0,
      targetHits: 0,
      latestQuoteDate: null,
      error: null,
    },
    {
      grade: 'CCI4500',
      dateFrom: '2026-11-04',
      dateTo: '2026-11-08',
      returned: null,
      targetHits: 0,
      latestQuoteDate: null,
      error: '网络请求失败',
    },
  ],
}

test('CCI recovery reports date windows and missing slots separately from today hits', async ({
  page,
}) => {
  const errors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  await page.route('**/api/v1/commodities/cci', (route) =>
    route.fulfill({ json: { ...data, status: recoveryStatus } }),
  )
  await page.goto('/commodities?instrument=cci')
  const status = page.getByRole('status', { name: 'CCI数据更新状态' })
  await expect(status).toContainText('更新范围：2026-11-04 至 2026-11-09')
  await expect(status).toContainText('返回 15 条报价 · 新增 12 · 变更 1')
  await expect(status).toContainText('今日（2026-11-09）命中 2/3 档')
  await expect(status).not.toContainText('15/3')
  await expect(status).toContainText(
    '尚缺 5 条 · 已尝试查询 15 条 · 来源暂无报价 2 条 · 本次未查询 3 条',
  )
  await expect(status).toContainText('CCI5500（2026-11-04 至 2026-11-08）：已获取来源报价')
  await expect(status).toContainText('CCI5500（2026-11-09 至 2026-11-09）：已获取来源报价')
  await expect(status).toContainText('CCI4500（2026-11-09 至 2026-11-09）：来源暂无报价')
  await expect(status).toContainText('CCI4500（2026-11-04 至 2026-11-08）：请求失败：网络请求失败')
  await expect(page.getByRole('row')).toHaveCount(587)
  await expect(page.getByRole('button', { name: '更新今日' })).toHaveCount(0)
  expect(errors.filter((message) => /same key|unique.*key/i.test(message))).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('CCI manual recovery polls until finished and preserves history', async ({ page }) => {
  let posts = 0
  let polls = 0
  await page.route('**/api/v1/commodities/cci**', (route) => {
    if (route.request().method() === 'POST') {
      posts++
      return route.fulfill({ json: { started: true } })
    }
    if (!posts) return route.fulfill({ json: data })
    polls++
    return route.fulfill({ json: { ...data, status: { ...recoveryStatus, running: polls < 2 } } })
  })
  await page.goto('/commodities?instrument=cci')
  await page.getByRole('button', { name: '更新数据' }).click()
  await expect(page.getByRole('button', { name: '更新中…' })).toBeDisabled()
  await expect(page.getByRole('row')).toHaveCount(587)
  await expect(page.getByRole('button', { name: '更新数据' })).toBeEnabled()
  expect(posts).toBe(1)
  expect(polls).toBe(2)
  await expect(page.getByRole('status', { name: 'CCI数据更新状态' })).toContainText('尚缺 5 条')
})

test('CCI recovery remains read-only for non-admin users', async ({ page }) => {
  await page.route('**/api/auth/**', (route) =>
    route.fulfill({
      json: {
        ...adminSession,
        user: { ...adminSession.user, role: 'viewer' },
      },
    }),
  )
  await page.route('**/api/v1/commodities/cci', (route) =>
    route.fulfill({ json: { ...data, status: recoveryStatus } }),
  )
  await page.goto('/commodities?instrument=cci')
  await expect(page.getByRole('status', { name: 'CCI数据更新状态' })).toBeVisible()
  await expect(page.getByRole('button', { name: '更新数据' })).toHaveCount(0)
  await expect(page.getByRole('row')).toHaveCount(587)
})

test('CCI explains initial recovery floor when no quote has succeeded', async ({ page }) => {
  await page.route('**/api/v1/commodities/cci', (route) =>
    route.fulfill({
      json: {
        ...data,
        status: {
          ...recoveryStatus,
          recovery: {
            ...recoveryStatus.recovery,
            initializedFrom: [
              { grade: 'CCI5000', dateFrom: '2026-11-04', origin: 'installation_date' },
            ],
          },
        },
      },
    }),
  )
  await page.goto('/commodities?instrument=cci')
  await expect(page.getByRole('status', { name: 'CCI数据更新状态' })).toContainText(
    '暂无历史报价的档位从首次采集记录日期开始：CCI5000 2026-11-04。',
  )
})
