import { adminSession, expect, test, type Page } from './fixtures'

const configuration = {
  revision: 1,
  username: 'writer',
  password_set: true,
  host: 'db.example',
  port: 3306,
  database: 'market',
  enabled: true,
  schedule: ['18:30'],
  source: 'etf',
  transport_secure: true,
  target_changed: false,
}
const idle = {
  enabled: true,
  configured: true,
  status: 'idle',
  timezone: 'Asia/Shanghai',
  schedule: ['18:30'],
  trade_date: null as string | null,
  run_id: null as string | null,
  total: 0,
  completed: 0,
  pending: 0,
  above_count: 0,
  heat_status: 'pending',
  crowding_status: 'pending',
  cache_pending: false,
  updated_at: null as string | null,
  error: null as string | null,
}
const heatPoints = [
  {
    date: '2026-11-06',
    ratio: 0.6,
    above_count: 60,
    eligible_count: 100,
    universe_count: 110,
    state: 'normal',
  },
  {
    date: '2026-11-09',
    ratio: 0.8,
    above_count: 80,
    eligible_count: 100,
    universe_count: 110,
    state: 'normal',
  },
  {
    date: '2026-11-10',
    ratio: 0.8,
    above_count: 80,
    eligible_count: 100,
    universe_count: 110,
    state: 'normal',
    source: 'sina',
    price_basis: 'qfq',
    denominator: 'eligible',
  },
]
const heat = {
  start_date: '2026-11-06',
  end_date: '2026-11-10',
  latest: { ...heatPoints[2], updated_at: '2026-11-10T19:30:00+08:00' },
  points: heatPoints,
  summary: {
    observed_days: 3,
    cold_days: 0,
    hot_days: 0,
    low_ratio: 0.6,
    peak_ratio: 0.8,
    average_ratio: 0.7333,
    change_pp: 20,
  },
}
const crowding = {
  top_pct: 0.05,
  start_date: '2026-11-06',
  end_date: '2026-11-10',
  latest: {
    date: '2026-11-10',
    ratio: 0.4,
    stock_count: 100,
    top_stock_count: 5,
    top_money: 400000000,
    total_money: 1000000000,
    updated_at: '2026-11-10T19:30:00+08:00',
    source: 'sina',
  },
  summary: {
    observed_days: 3,
    average_ratio: 0.35,
    peak_ratio: 0.4,
    low_ratio: 0.3,
    change_pp: 10,
  },
  points: [
    { date: '2026-11-06', ratio: 0.3, smoothed_ratio: 0.3 },
    { date: '2026-11-09', ratio: 0.35, smoothed_ratio: 0.3275 },
    { date: '2026-11-10', ratio: 0.4, smoothed_ratio: 0.4, source: 'sina' },
  ],
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/cache/status', (route) =>
    route.fulfill({ json: { enabled: false, modules: [] } }),
  )
  await page.route('**/api/v1/market-collection/status', (route) => route.fulfill({ json: idle }))
  await page.route('**/api/v1/market-collection/configuration', (route) =>
    route.fulfill({ json: configuration }),
  )
  await page.route('**/api/v1/market-sentiment/history?*', (route) => route.fulfill({ json: heat }))
  await page.route('**/api/v1/market-crowding/history?*', (route) =>
    route.fulfill({ json: crowding }),
  )
})

for (const { path, pageClass, history, emptyTitle, chart } of [
  {
    path: '/market-sentiment',
    pageClass: '.sentiment-page',
    history: heat,
    emptyTitle: '暂无市场热度数据',
    chart: '.sentiment-chart canvas',
  },
  {
    path: '/market-crowding',
    pageClass: '.crowding-page',
    history: crowding,
    emptyTitle: '暂无拥挤度数据',
    chart: '.trend-chart canvas',
  },
]) {
  for (const state of ['populated', 'loading', 'error', 'empty'] as const) {
    test(`places collection last after methodology: ${path} ${state}`, async ({ page }) => {
      let releaseHistory = () => {}
      const historyReady = new Promise<void>((resolve) => {
        releaseHistory = resolve
      })
      await page.route(`**/api/v1${path}/history?*`, async (route) => {
        if (state === 'loading') await historyReady
        await route.fulfill(
          state === 'error'
            ? { status: 503, json: { detail: 'History unavailable' } }
            : {
                json: state === 'empty' ? { ...history, latest: null, points: [] } : history,
              },
        )
      })
      try {
        await page.goto(path)
        const root = page.locator(pageClass)
        if (state === 'populated') await expect(root.locator(chart)).toBeVisible()
        if (state === 'loading') await expect(root.locator('.chart-skeleton')).toBeVisible()
        if (state === 'error') {
          await expect(root.getByRole('alert')).toContainText('History unavailable')
        }
        if (state === 'empty') {
          await expect(root.getByRole('heading', { name: emptyTitle })).toBeVisible()
        }

        const panel = root.getByRole('region', { name: '市场日采集' })
        await expect(panel).toHaveCount(1)
        await expect(
          page.locator(`${pageClass} > .method-card + .market-collection-panel:last-child`),
        ).toHaveCount(1)
        await panel.scrollIntoViewIfNeeded()
        await expect(panel).toBeVisible()
        const layout = await root.evaluate((element) => {
          const method = element.querySelector('.method-card')!
          const collection = element.querySelector('.market-collection-panel')!
          return {
            gap: collection.getBoundingClientRect().top - method.getBoundingClientRect().bottom,
            marginTop: getComputedStyle(collection).marginTop,
            fitsViewport: document.documentElement.scrollWidth <= innerWidth,
          }
        })
        expect(layout.gap).toBeCloseTo(24, 0)
        expect(layout.marginTop).toBe('24px')
        expect(layout.fitsViewport).toBe(true)
      } finally {
        releaseHistory()
      }
    })
  }
}

// The dev server resolves this test-only module through the same Vite import
// graph as the components. Do not discover it from Resource Timing: fake clocks
// and timing-buffer eviction can remove those entries despite a live chart.
async function waitForChart(page: Page, selector: string) {
  await expect
    .poll(async () =>
      page.evaluate(async (css) => {
        const runtimePath = '/tests/chart-runtime.ts'
        const { getInstanceByDom } = await import(runtimePath)
        const element = document.querySelector(css)
        const chart = element && getInstanceByDom(element)
        const option = chart?.getOption()
        return !!(
          chart &&
          !chart.isDisposed() &&
          option?.dataZoom?.length &&
          option?.series?.length &&
          typeof option?.tooltip?.[0]?.formatter === 'function'
        )
      }, selector),
    )
    .toBe(true)
}

async function inspectChart(page: Page, selector: string) {
  await waitForChart(page, selector)
  return page.evaluate(async (css) => {
    const runtimePath = '/tests/chart-runtime.ts'
    const { getInstanceByDom } = await import(runtimePath)
    const chart = getInstanceByDom(document.querySelector(css))
    const option = chart.getOption()
    return {
      zoom: { start: option.dataZoom[0].start, end: option.dataZoom[0].end },
      series: option.series.map(
        (line: { name: string; data: unknown[]; connectNulls: boolean }) => ({
          name: line.name,
          data: line.data,
          connectNulls: line.connectNulls,
        }),
      ),
      firstTooltip: option.tooltip[0].formatter([{ dataIndex: 0 }]),
      lastTooltip: option.tooltip[0].formatter([{ dataIndex: 2 }]),
    }
  }, selector)
}

test('uses the original methodology with Sina data, preserves older metadata and shares management across both pages', async ({
  page,
}) => {
  await page.goto('/market-sentiment')
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
  await expect(page.locator('.market-source-note')).toContainText('新浪接口 · 前复权MA20')
  await expect(page.locator('.market-source-note')).toContainText('以有效股票数为分母')
  await expect(page.locator('.stats-grid')).toContainText('有效样本 100 只 · 股票总数 110 只')
  await expect(page.locator('.sentiment-state')).toHaveText('正常区间')
  await expect(page.locator('.stats-grid')).toContainText('80.00%')
  await expect(page.locator('.method-card')).toContainText('按聚宽原脚本口径')
  await expect(page.locator('.method-card')).toContainText(
    '当日停牌及不足20日均值的股票不计入有效样本',
  )
  await expect(page.locator('.method-card')).toContainText('历史停牌日沿用前收盘价')
  await expect(page.locator('.method-card')).toContainText('原脚本容差')
  const chart = await inspectChart(page, '.sentiment-chart')
  expect(
    chart.series
      .filter((line: { name: string }) => line.name === '站上MA20比例')
      .map((line: { data: unknown[] }) => line.data),
  ).toEqual([
    [60, 80, null],
    [null, null, 80],
  ])
  expect(chart.firstTooltip).toContain('聚宽历史 · 前复权MA20')
  expect(chart.firstTooltip).toContain('有效股票 100 只')
  expect(chart.lastTooltip).toContain('新浪接口 · 前复权MA20')
  expect(chart.lastTooltip).toContain('有效股票 100 只')
  await expect(page.getByRole('region', { name: '市场日采集' })).toContainText('18:30')
  const previousMethod = {
    ...heatPoints[2],
    price_basis: 'bfq',
    denominator: 'universe',
    eligible_count: 90,
    universe_count: 100,
  }
  await page.route('**/api/v1/market-sentiment/history?*', (route) =>
    route.fulfill({
      json: {
        ...heat,
        latest: { ...heat.latest, ...previousMethod },
        points: [...heatPoints.slice(0, 2), previousMethod],
      },
    }),
  )
  await page.getByRole('button', { name: '刷新市场热度数据' }).click()
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
  await expect(page.locator('.market-source-note')).toContainText('新浪接口 · 不复权MA20')
  await expect(page.locator('.market-source-note')).toContainText('以全A股票总数为分母')
  await expect(page.locator('.stats-grid')).toContainText('股票总数 100 只 · 有效MA20 90 只')
  await expect(page.locator('.method-card')).toContainText('既有记录的已存口径')
  expect((await inspectChart(page, '.sentiment-chart')).lastTooltip).toContain('股票总数 100 只')
  await page.getByRole('link', { name: '抱团拥挤度', exact: true }).click()
  await expect(page.locator('.crowding-page .trend-chart canvas')).toBeVisible()
  await expect(page.locator('.market-source-note')).toContainText('新浪接口 · 全A成交额')
  await expect(page.locator('.method-card')).toContainText('聚宽原脚本的前5%成交额占比算法')
  const crowdingChart = await inspectChart(page, '.crowding-page .trend-chart')
  expect(
    crowdingChart.series
      .filter((line: { name: string }) => line.name === '原始占比')
      .map((line: { data: unknown[] }) => line.data),
  ).toEqual([
    [30, 35, null],
    [null, null, 40],
  ])
  expect(
    crowdingChart.series.filter((line: { name: string }) => line.name === 'EMA 10 平滑').at(-1)
      .data,
  ).toEqual([null, null, 40])
  await expect(page.locator('.stats-grid')).toContainText('跨来源区间不计算整体变化')
  await page.getByRole('checkbox', { name: '显示 EMA 10 平滑参考线' }).uncheck()
  expect(
    (await inspectChart(page, '.crowding-page .trend-chart')).series.filter(
      (line: { name: string }) => line.name === 'EMA 10 平滑',
    ),
  ).toHaveLength(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('saves one fixed-target account, keeps omitted passwords, tests read-only and fits narrow screens', async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 740 })
  const bodies: Record<string, unknown>[] = []
  let saved = { ...configuration }
  await page.route('**/api/v1/market-collection/configuration', (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON()
      bodies.push(body)
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      saved = {
        ...saved,
        revision: body.revision + 1,
        username: body.username,
        enabled: body.enabled,
      }
    }
    return route.fulfill({ json: saved })
  })
  await page.route('**/api/v1/market-collection/test', (route) =>
    route.fulfill({
      json: { ok: true, permissions_verified: false, message: '连接与表读取通过。' },
    }),
  )
  await page.goto('/market-sentiment')
  await page.getByText('日采集配置', { exact: true }).click()
  const panel = page.getByRole('region', { name: '市场日采集' })
  await expect(panel).toContainText('db.example:3306 / market')
  await expect(panel).toContainText('已使用现有 ETF 采集写入账户')
  await expect(panel.locator('input[type="time"]')).toHaveCount(0)
  await expect(page.getByLabel('数据库主机', { exact: true })).toHaveCount(0)
  await page.getByLabel('启用市场自动采集').uncheck()
  await page.getByRole('button', { name: '保存日采集配置', exact: true }).click()
  await expect(panel).toContainText('日采集配置已保存')
  expect(bodies[0]).toEqual({ revision: 1, username: 'writer', enabled: false })
  await page.getByRole('button', { name: '测试数据库连接（只读）' }).click()
  await expect(panel).toContainText('实际写入结果以采集任务为准')
  await page.getByLabel('采集写入账号', { exact: true }).fill('replacement')
  await expect(page.getByRole('button', { name: '保存日采集配置', exact: true })).toBeDisabled()
  await page.getByLabel('数据库密码', { exact: true }).fill('fixture-only-password')
  await page.getByRole('button', { name: '保存日采集配置', exact: true }).click()
  await expect(page.getByLabel('数据库密码', { exact: true })).toHaveValue('')
  expect(bodies[1]).toEqual({
    revision: 2,
    username: 'replacement',
    enabled: false,
    password: 'fixture-only-password',
  })
  expect(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  for (const input of await panel.locator('.market-collection-fields input').all()) {
    const box = await input.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(320)
  }
})

test('blocks HTTP credential changes and requires a fresh password for a changed target', async ({
  page,
}) => {
  let response = { ...configuration, transport_secure: false, target_changed: false }
  await page.route('**/api/v1/market-collection/configuration', (route) =>
    route.fulfill({ json: response }),
  )
  await page.goto('/market-crowding')
  await page.getByText('日采集配置', { exact: true }).click()
  await expect(page.getByText('请通过 HTTPS 访问后保存或测试数据库凭据。')).toBeVisible()
  await expect(page.getByLabel('数据库密码', { exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '保存日采集配置', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '测试数据库连接（只读）' })).toBeDisabled()
  response = { ...configuration, transport_secure: true, target_changed: true }
  await page.reload()
  await page.getByText('日采集配置', { exact: true }).click()
  await expect(page.getByText('数据库目标已变更，请输入当前目标的写入密码后保存。')).toBeVisible()
  await expect(page.getByRole('button', { name: '保存日采集配置', exact: true })).toBeDisabled()
  await page.getByLabel('数据库密码', { exact: true }).fill('fixture-new-target')
  await expect(page.getByRole('button', { name: '保存日采集配置', exact: true })).toBeEnabled()
})

test('viewers see automatic collection progress without reading configuration or mutation controls', async ({
  page,
}) => {
  let configReads = 0
  await page.route('**/api/auth/**', (route) =>
    route.fulfill({
      json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } },
    }),
  )
  await page.route('**/api/v1/market-collection/configuration', (route) => {
    configReads++
    return route.fulfill({ json: configuration })
  })
  await page.route('**/api/v1/market-collection/status', (route) =>
    route.fulfill({
      json: {
        ...idle,
        status: 'running',
        trade_date: '2026-11-10',
        total: 100,
        completed: 5,
        pending: 95,
        progress: { phase: 'ma20', total: 100, completed: 5, pending: 95 },
      },
    }),
  )
  await page.goto('/market-sentiment')
  const panel = page.getByRole('region', { name: '市场日采集' })
  await expect(panel).toContainText('已处理 5 / 100 只')
  await expect(page.getByRole('progressbar', { name: '市场日采集进度' })).toHaveAttribute(
    'value',
    '5',
  )
  await expect(panel.getByRole('button')).toHaveCount(0)
  await expect(panel.locator('input')).toHaveCount(0)
  await expect(page.getByText('日采集配置', { exact: true })).toHaveCount(0)
  expect(configReads).toBe(0)
})

test('a disabled server worker cannot queue collection even with a configured account', async ({
  page,
}) => {
  let runs = 0
  await page.route('**/api/v1/market-collection/status', (route) =>
    route.fulfill({
      json: {
        ...idle,
        configured: true,
        ready: false,
        worker_disabled: true,
        status: 'worker_disabled',
        error: 'worker_disabled',
      },
    }),
  )
  await page.route('**/api/v1/market-collection/run', (route) => {
    runs++
    return route.fulfill({ status: 202, json: {} })
  })
  await page.goto('/market-sentiment')
  const panel = page.getByRole('region', { name: '市场日采集' })
  await expect(panel).toContainText('服务端采集任务未启用')
  await expect(panel).toContainText('自动计划待服务启用')
  const start = page.getByRole('button', { name: '立即采集', exact: true })
  await expect(start).toBeDisabled()
  await start.evaluate((element: HTMLButtonElement) => element.click())
  expect(runs).toBe(0)
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
})

for (const scenario of [
  {
    name: 'blocks unverified heat while crowding publishes independently',
    error: 'heat_methodology_unverified',
    crowdingStatus: 'written',
    crowdingLabel: '已入库',
    explanation: '新浪市场热度尚未通过聚宽原脚本口径核验，暂不发布',
  },
  {
    name: 'keeps saved charts when incoming methodology validation fails',
    error: 'writer_methodology_mismatch',
    crowdingStatus: 'blocked',
    crowdingLabel: '未通过校验，未发布',
    explanation: '本次结果的统计口径未通过校验，未写入；已有数据保留。',
  },
]) {
  test(scenario.name, async ({ page }) => {
    const previousHistory = {
      ...heat,
      end_date: '2026-11-09',
      points: heatPoints.slice(0, 2),
      latest: { ...heatPoints[1], updated_at: '2026-11-09T19:00:00+08:00' },
    }
    await page.route('**/api/v1/market-sentiment/history?*', (route) =>
      route.fulfill({ json: previousHistory }),
    )
    await page.route('**/api/v1/market-collection/status', (route) =>
      route.fulfill({
        json: {
          ...idle,
          status: 'blocked',
          trade_date: '2026-11-10',
          run_id: 'blocked-run',
          heat_status: 'blocked',
          crowding_status: scenario.crowdingStatus,
          error: scenario.error,
        },
      }),
    )
    await page.goto('/market-sentiment')
    const panel = page.getByRole('region', { name: '市场日采集' })
    await expect(panel).toContainText('以聚宽原脚本为目标口径')
    await expect(panel).toContainText('热度与拥挤度分别核验发布，证据不足的缺口保留。')
    await expect(panel).toContainText('市场热度 · 未通过校验，未发布')
    await expect(panel).toContainText(`抱团拥挤度 · ${scenario.crowdingLabel}`)
    await expect(panel).toContainText(scenario.explanation)
    await expect(page.locator('.market-source-note')).toContainText('聚宽历史 · 前复权MA20')
    await expect(page.locator('.stats-grid')).toContainText('80.00%')
    await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
    await expect(page.getByRole('button', { name: '重试未完成采集', exact: true })).toBeEnabled()
  })
}

test('the first published status replaces an older history response still in flight exactly once', async ({
  page,
}) => {
  await page.clock.install()
  let published = false
  let oldReads = 0
  let freshReads = 0
  let releaseStatus!: () => void
  let releaseHistory!: () => void
  const statusGate = new Promise<void>((resolve) => {
    releaseStatus = resolve
  })
  const historyGate = new Promise<void>((resolve) => {
    releaseHistory = resolve
  })
  await page.route('**/api/v1/market-sentiment/history?*', async (route) => {
    if (!published) {
      oldReads++
      await historyGate
      await route
        .fulfill({ json: { ...heat, latest: null, points: [], summary: { observed_days: 0 } } })
        .catch(() => {})
      return
    }
    freshReads++
    await route.fulfill({ json: heat })
  })
  await page.route('**/api/v1/market-collection/status', async (route) => {
    await statusGate
    await route
      .fulfill({
        json: {
          ...idle,
          status: 'complete',
          run_id: 'published-at-load',
          trade_date: '2026-11-10',
          heat_status: 'written',
          crowding_status: 'written',
          updated_at: '2026-11-10T19:30:00+08:00',
        },
      })
      .catch(() => {})
  })
  try {
    await page.goto('/market-sentiment')
    await expect.poll(() => oldReads).toBeGreaterThan(0)
    await expect(page.getByRole('region', { name: '市场日采集' })).toContainText('正在读取采集状态')
    published = true
    releaseStatus()
    await expect.poll(() => freshReads).toBe(1)
    await expect(page.locator('.stats-grid')).toContainText('80.00%')
    releaseHistory()
    await page.clock.fastForward(60000)
    await expect(page.locator('.stats-grid')).toContainText('80.00%')
    expect(freshReads).toBe(1)
  } finally {
    releaseStatus()
    releaseHistory()
  }
})

test('locks repeated starts, keeps action failures across polling and retries the same request id', async ({
  page,
}) => {
  await page.clock.install()
  const ids: string[] = []
  let state = { ...idle }
  let release!: () => void
  const hold = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/market-collection/status', (route) => route.fulfill({ json: state }))
  await page.route('**/api/v1/market-collection/run', async (route) => {
    ids.push(route.request().postDataJSON().request_id)
    expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
    if (ids.length === 1) {
      await hold
      await route.abort()
      return
    }
    state = { ...idle, status: 'running', run_id: ids[0], total: 100, pending: 100 }
    await route.fulfill({ status: 202, json: { id: ids[0], status: 'queued' } })
  })
  await page.goto('/market-sentiment')
  const start = page.getByRole('button', { name: '立即采集', exact: true })
  await expect(start).toBeEnabled()
  await start.evaluate((element: HTMLButtonElement) => {
    element.click()
    element.click()
  })
  await expect(page.getByRole('button', { name: '提交中…', exact: true })).toBeDisabled()
  await expect.poll(() => ids.length).toBe(1)
  release()
  const retry = page.getByRole('button', { name: '重试同一采集请求', exact: true })
  await expect(retry).toBeEnabled()
  await page.clock.fastForward(30000)
  await expect(page.getByRole('alert')).toContainText('重试将沿用同一请求')
  await retry.click()
  await expect.poll(() => ids.length).toBe(2)
  expect(ids[0]).toMatch(/^[0-9a-f-]{36}$/)
  expect(ids[1]).toBe(ids[0])
  await expect(page.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
})

test('progress polls preserve chart zoom and reload only once after published cache completion', async ({
  page,
}) => {
  await page.clock.install()
  let histories = 0
  let polls = 0
  let state = {
    ...idle,
    status: 'running',
    run_id: 'job-one',
    trade_date: '2026-11-10',
    total: 100,
    completed: 10,
    pending: 90,
    crowding_status: 'written',
    updated_at: '2026-11-10T18:10:00+08:00',
  }
  await page.route('**/api/v1/market-sentiment/history?*', (route) => {
    histories++
    return route.fulfill({ json: heat })
  })
  await page.route('**/api/v1/market-collection/status', (route) => {
    polls++
    return route.fulfill({ json: state })
  })
  await page.goto('/market-sentiment')
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
  await expect(page.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
  await waitForChart(page, '.sentiment-chart')
  await page.evaluate(async () => {
    const runtimePath = '/tests/chart-runtime.ts'
    const { getInstanceByDom } = await import(runtimePath)
    getInstanceByDom(document.querySelector('.sentiment-chart')).dispatchAction({
      type: 'dataZoom',
      start: 25,
      end: 90,
    })
  })
  const initialHistories = histories
  const initialPolls = polls
  const zoom = (await inspectChart(page, '.sentiment-chart')).zoom
  state = { ...state, completed: 20, pending: 80, updated_at: '2026-11-10T18:15:00+08:00' }
  await page.clock.fastForward(1600)
  await expect.poll(() => polls).toBeGreaterThan(initialPolls)
  expect(histories).toBe(initialHistories)
  expect((await inspectChart(page, '.sentiment-chart')).zoom).toEqual(zoom)
  state = {
    ...state,
    status: 'complete',
    completed: 100,
    pending: 0,
    heat_status: 'written',
    cache_pending: true,
    updated_at: '2026-11-10T19:30:00+08:00',
  }
  await page.clock.fastForward(1600)
  await expect(page.getByText('结果已入库，等待图表数据更新…')).toBeVisible()
  expect(histories).toBe(initialHistories)
  state = { ...state, cache_pending: false }
  await page.clock.fastForward(1600)
  await expect.poll(() => histories).toBe(initialHistories + 1)
  await expect(page.locator('.sentiment-chart canvas')).toBeVisible()
  await page.clock.fastForward(60000)
  expect(histories).toBe(initialHistories + 1)
  await page.getByRole('link', { name: '总览', exact: true }).click()
  await expect(page.getByRole('region', { name: '市场日采集' })).toHaveCount(0)
  const beforeLeave = polls
  await page.clock.fastForward(60000)
  expect(polls).toBe(beforeLeave)
})

test('shows independent source dates and missing-day details with the manual cutoff', async ({
  page,
}) => {
  await page.route('**/api/v1/market-collection/status', (route) =>
    route.fulfill({
      json: {
        ...idle,
        status: 'partial',
        source_latest: { heat: '2026-11-10', crowding: '2026-11-06' },
        gaps: [
          {
            trade_date: '2026-11-09',
            status: 'blocked',
            heat_status: 'outside_window',
            crowding_status: 'blocked',
            error: 'historical_snapshot_missing',
          },
        ],
      },
    }),
  )
  await page.goto('/market-sentiment')
  const panel = page.getByRole('region', { name: '市场日采集' })
  await expect(panel).toContainText('18:30前手动触发仅处理此前已结束的交易日')
  await expect(panel).toContainText('首轮先完整处理全部待采集数据，失败项先跳过')
  await expect(panel).toContainText('首轮结束后，仅对失败项最多再试3轮')
  await expect(panel).toContainText('仍失败的缺口保留到下次采集继续处理，成功记录不重复发布')
  await expect(panel).toContainText('并继续处理已登记的失败缺口')
  await expect(panel).toContainText('未登记的更早历史缺口不自动补采')
  await panel.getByText('日采集配置', { exact: true }).click()
  await expect(panel).toContainText('同一采集请求或同一计划内重复检查、服务重启均不重置次数')
  await expect(panel).toContainText('新的手动采集请求或下一轮18:30计划会重新开始计次')
  await expect(panel).toContainText('已登记的失败缺口持续保留，不因后续日期采集成功而丢弃')
  await expect(panel).toContainText('热度截至 2026-11-10')
  await expect(panel).toContainText('拥挤度截至 2026-11-06')
  await panel.getByText('待补齐交易日（最近 1 条）').click()
  await expect(panel).toContainText('2026-11-09 · 热度：不在本次补缺范围')
  await expect(panel).toContainText('缺少目标交易日的完整快照')
})

for (const legacy of [false, true]) {
  test(`a ${legacy ? 'legacy' : 'structured'} busy rejection follows shared work instead of offering uncertain retry`, async ({
    page,
  }) => {
    await page.clock.install()
    const ids: string[] = []
    let state = { ...idle, running: false }
    await page.route('**/api/v1/market-collection/status', (route) =>
      route.fulfill({ json: state }),
    )
    await page.route('**/api/v1/market-collection/run', (route) => {
      ids.push(route.request().postDataJSON().request_id)
      if (ids.length === 1) {
        state = { ...idle, status: 'partial', running: true, run_id: 'shared-job' }
        return route.fulfill({
          status: 409,
          json: {
            detail: legacy
              ? '已有采集运行，请等待完成或暂停后重试'
              : {
                  code: 'collection_busy',
                  message: '已有采集运行，请等待当前采集结束',
                  active_run: { id: 'shared-job', status: 'running' },
                },
          },
        })
      }
      state = { ...idle, running: true, status: 'running', run_id: ids[1] }
      return route.fulfill({ status: 202, json: { id: ids[1], status: 'queued' } })
    })
    await page.goto('/market-sentiment')
    const panel = page.getByRole('region', { name: '市场日采集' })
    await panel.getByRole('button', { name: '立即采集', exact: true }).click()
    await expect(panel.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
    await expect(panel.getByRole('alert')).toHaveCount(0)
    await expect(panel).not.toContainText('重试将沿用同一请求')
    await expect(panel.getByRole('button', { name: '重试同一采集请求', exact: true })).toHaveCount(
      0,
    )
    await panel.getByText('日采集配置', { exact: true }).click()
    await expect(panel.getByLabel('采集写入账号')).toBeDisabled()
    state = { ...state, running: false, status: 'complete' }
    await page.clock.fastForward(1600)
    const start = panel.getByRole('button', { name: '立即采集', exact: true })
    await expect(start).toBeEnabled()
    await expect(panel).not.toContainText('已有采集正在运行')
    await start.click()
    await expect.poll(() => ids.length).toBe(2)
    expect(ids[1]).not.toBe(ids[0])
    await expect(panel.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
  })
}

for (const dayStatus of ['partial', 'blocked']) {
  test(`the shared worker lock blocks starts even while the day remains ${dayStatus}`, async ({
    page,
  }) => {
    let starts = 0
    await page.route('**/api/v1/market-collection/status', (route) =>
      route.fulfill({ json: { ...idle, status: dayStatus, running: true, run_id: 'shared-job' } }),
    )
    await page.route('**/api/v1/market-collection/run', (route) => {
      starts++
      return route.fulfill({ status: 202, json: {} })
    })
    await page.goto('/market-sentiment')
    const panel = page.getByRole('region', { name: '市场日采集' })
    const start = panel.getByRole('button', { name: '采集中…', exact: true })
    await expect(start).toBeDisabled()
    await start.evaluate((button: HTMLButtonElement) => {
      button.click()
      button.click()
    })
    expect(starts).toBe(0)
    await expect(panel).toContainText('首轮失败项先跳过，完整采集一遍后仅重试失败项（最多3轮）')
    await expect(panel.getByRole('button', { name: '重试未完成采集', exact: true })).toHaveCount(0)
  })
}

test('an acknowledged run remains locked across stale or failed status reads', async ({ page }) => {
  await page.clock.install()
  const ids: string[] = []
  let state = { ...idle }
  let failStatus = false
  let statusReads = 0
  await page.route('**/api/v1/market-collection/status', (route) => {
    statusReads++
    return failStatus ? route.fulfill({ status: 503, json: {} }) : route.fulfill({ json: state })
  })
  await page.route('**/api/v1/market-collection/run', (route) => {
    ids.push(route.request().postDataJSON().request_id)
    return route.fulfill({ status: 202, json: { id: ids[0], status: 'queued' } })
  })
  await page.goto('/market-sentiment')
  const panel = page.getByRole('region', { name: '市场日采集' })
  await panel.getByRole('button', { name: '立即采集', exact: true }).click()
  const confirming = panel.getByRole('button', { name: '采集中…', exact: true })
  await expect(confirming).toBeDisabled()
  await expect.poll(() => statusReads).toBeGreaterThanOrEqual(2)
  await confirming.evaluate((button: HTMLButtonElement) => {
    button.click()
    button.click()
  })
  expect(ids).toHaveLength(1)
  failStatus = true
  await page.clock.fastForward(1600)
  await expect(panel).toContainText('暂时无法读取采集状态')
  await expect(confirming).toBeDisabled()
  failStatus = false
  state = { ...idle, status: 'running', run_id: ids[0] }
  await panel.getByRole('button', { name: '重新读取状态', exact: true }).click()
  await expect(panel.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
  expect(ids).toHaveLength(1)
})

test('a busy retry cannot discard an earlier uncertain id even after another job completes', async ({
  page,
}) => {
  await page.clock.install()
  const ids: string[] = []
  let state = { ...idle }
  await page.route('**/api/v1/market-collection/status', (route) => route.fulfill({ json: state }))
  await page.route('**/api/v1/market-collection/run', (route) => {
    ids.push(route.request().postDataJSON().request_id)
    if (ids.length === 1) return route.abort()
    if (ids.length === 2) {
      state = { ...idle, status: 'running', run_id: 'shared-job' }
      return route.fulfill({
        status: 409,
        json: {
          detail: {
            code: 'collection_busy',
            message: '已有采集运行',
            active_run: { id: 'shared-job', status: 'running' },
          },
        },
      })
    }
    state = { ...idle, status: 'running', run_id: ids[0] }
    return route.fulfill({ status: 202, json: { id: ids[0], status: 'queued' } })
  })
  await page.goto('/market-sentiment')
  const panel = page.getByRole('region', { name: '市场日采集' })
  await panel.getByRole('button', { name: '立即采集', exact: true }).click()
  const retry = panel.getByRole('button', { name: '重试同一采集请求', exact: true })
  await expect(retry).toBeEnabled()
  await retry.click()
  await expect(panel.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
  await expect(panel.getByRole('alert')).toHaveCount(0)
  await expect(panel).not.toContainText('重试将沿用同一请求')
  state = { ...state, status: 'complete' }
  await page.clock.fastForward(1600)
  await expect(retry).toBeEnabled()
  await expect(panel).toContainText('上次请求的结果仍待确认')
  await retry.click()
  await expect.poll(() => ids.length).toBe(3)
  expect(new Set(ids).size).toBe(1)
  await expect(panel.getByRole('button', { name: '采集中…', exact: true })).toBeDisabled()
})

for (const firstResult of ['lost', 'malformed']) {
  test(`a ${firstResult} response followed by completed replay preserves the id and never claims a new job`, async ({
    page,
  }) => {
    const ids: string[] = []
    let state = { ...idle }
    await page.route('**/api/v1/market-collection/status', (route) =>
      route.fulfill({ json: state }),
    )
    await page.route('**/api/v1/market-collection/run', (route) => {
      ids.push(route.request().postDataJSON().request_id)
      if (ids.length === 1)
        return firstResult === 'lost' ? route.abort() : route.fulfill({ status: 202, body: '{' })
      state = { ...idle, status: 'complete', run_id: ids[0] }
      return route.fulfill({ status: 202, json: { id: ids[0], status: 'complete' } })
    })
    await page.goto('/market-sentiment')
    const panel = page.getByRole('region', { name: '市场日采集' })
    await panel.getByRole('button', { name: '立即采集', exact: true }).click()
    await panel.getByRole('button', { name: '重试同一采集请求', exact: true }).click()
    await expect(panel).toContainText('该采集请求已完成，未重复启动任务。')
    await expect(panel).not.toContainText('采集请求已提交')
    await expect(panel.getByRole('alert')).toHaveCount(0)
    await expect(panel.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
    expect(ids).toHaveLength(2)
    expect(ids[1]).toBe(ids[0])
  })
}

test('polling finds a completed uncertain request in history even after a newer run', async ({
  page,
}) => {
  await page.clock.install()
  let id = ''
  let resolved = false
  await page.route('**/api/v1/market-collection/status', (route) =>
    route.fulfill({
      json: resolved
        ? {
            ...idle,
            status: 'complete',
            run_id: 'newer-job',
            manual: { id: 'newer-job', status: 'complete' },
            runs: [
              { id: 'newer-job', status: 'complete' },
              { id, status: 'complete' },
            ],
          }
        : idle,
    }),
  )
  await page.route('**/api/v1/market-collection/run', (route) => {
    id = route.request().postDataJSON().request_id
    return route.abort()
  })
  await page.goto('/market-sentiment')
  const panel = page.getByRole('region', { name: '市场日采集' })
  await panel.getByRole('button', { name: '立即采集', exact: true }).click()
  await expect(panel.getByRole('button', { name: '重试同一采集请求', exact: true })).toBeEnabled()
  resolved = true
  await page.clock.fastForward(1600)
  await expect(panel.getByRole('button', { name: '立即采集', exact: true })).toBeEnabled()
  await expect(panel).toContainText('该采集请求已完成，未重复启动任务。')
  await expect(panel.getByRole('alert')).toHaveCount(0)
})

for (const status of [409, 422]) {
  test(`a definite HTTP ${status} rejection does not offer same-request recovery`, async ({
    page,
  }) => {
    const ids: string[] = []
    await page.route('**/api/v1/market-collection/run', (route) => {
      ids.push(route.request().postDataJSON().request_id)
      return route.fulfill({ status, json: { detail: '采集配置尚未就绪，请先保存数据库写入账号' } })
    })
    await page.goto('/market-sentiment')
    const panel = page.getByRole('region', { name: '市场日采集' })
    const start = panel.getByRole('button', { name: '立即采集', exact: true })
    await start.click()
    await expect(panel.getByRole('alert')).toContainText('采集配置尚未就绪')
    await expect(panel).not.toContainText('重试将沿用同一请求')
    await expect(panel.getByRole('button', { name: '重试同一采集请求', exact: true })).toHaveCount(
      0,
    )
    await expect(start).toBeEnabled()
    await start.click()
    await expect.poll(() => ids.length).toBe(2)
    expect(ids[1]).not.toBe(ids[0])
  })
}

for (const result of ['complete', 'partial', 'paused']) {
  test(`a replayed ${result} request unlocks even when it is no longer in recent status history`, async ({
    page,
  }) => {
    await page.clock.install()
    const ids: string[] = []
    await page.route('**/api/v1/market-collection/status', (route) =>
      route.fulfill({
        json: {
          ...idle,
          run_id: 'newer-job',
          manual: { id: 'newer-job', status: 'complete' },
          runs: [{ id: 'newer-job', status: 'complete' }],
        },
      }),
    )
    await page.route('**/api/v1/market-collection/run', (route) => {
      ids.push(route.request().postDataJSON().request_id)
      if (ids.length === 1) return route.abort()
      return route.fulfill({ status: 202, json: { id: ids[0], status: result } })
    })
    await page.goto('/market-sentiment')
    const panel = page.getByRole('region', { name: '市场日采集' })
    const start = panel.getByRole('button', { name: '立即采集', exact: true })
    await start.click()
    await panel.getByRole('button', { name: '重试同一采集请求', exact: true }).click()
    await expect(start).toBeEnabled()
    await expect(panel).toContainText('未重复启动任务')
    await expect(panel.getByRole('button', { name: '正在确认采集状态…', exact: true })).toHaveCount(
      0,
    )
    await page.clock.fastForward(60000)
    await expect(start).toBeEnabled()
    await expect(panel.getByRole('alert')).toHaveCount(0)
    expect(ids).toHaveLength(2)
    expect(ids[1]).toBe(ids[0])
  })
}
