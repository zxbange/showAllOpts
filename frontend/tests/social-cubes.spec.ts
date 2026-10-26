import { expect, test } from '@playwright/test'

test('manage cube monitoring and inspect rebalances within the configured time window', async ({
  page,
}, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  type Cube = {
    id: number
    symbol: string
    name: string
    cube_name: string
    owner_name: string
    enabled: number
    initialized: number
    last_check: string | null
    last_success_at: string | null
    last_error: string | null
    scan_page: number
  }
  const status = {
    enabled: false,
    interval_min: 600,
    interval_max: 960,
    night_rest: false,
    platforms: {} as Record<
      string,
      { paused: boolean; reason: string; alert_sent_at: string | null }
    >,
    last_check: null,
    next_check: null,
    last_error: null,
    token_configured: true,
    chat_id_configured: true,
    weibo_cookie_configured: true,
    xueqiu_cookie_configured: true,
    authors: [] as {
      id: number
      platform: 'weibo' | 'xueqiu'
      uid: string
      name: string
      enabled: number
      last_check: string | null
      last_error: string | null
    }[],
    counts: {},
    cubes: [] as Cube[],
    cube_counts: { sent: 1, baseline: 1 },
    cube_monitoring_active: false,
  }
  let nextId = 1
  let deleteRequests = 0
  const reads: string[] = []
  function records(cube: Cube) {
    return Array.from({ length: cube.id === 1 ? 21 : 1 }, (_, index) => ({
      id: cube.id * 100 + index,
      cube_id: cube.id,
      cube_name: cube.cube_name || cube.name,
      cube_alias: cube.name,
      owner_name: cube.owner_name,
      cube_symbol: cube.symbol,
      rebalance_id: String(cube.id * 1000 + index),
      status: 'success',
      notify_status: cube.id === 1 ? 'sent' : 'baseline',
      occurred_at: '2026-10-25T10:25:32+08:00',
      discovered_at: '2026-10-25T10:28:00+08:00',
      sent_at: cube.id === 1 ? '2026-10-25T10:28:03+08:00' : null,
      last_error: null,
      payload: {
        time: '2026-10-25T10:25:32+08:00',
        comment: index === 20 ? '第二页调仓' : '调仓备注 <script>不会执行</script>',
        // The UI constructs a fixed-domain link from the validated symbol.
        url: 'javascript:alert(1)',
        changes: [
          {
            symbol: 'SZ000792',
            name: '盐湖股份',
            price: 26.53,
            before: 11.4,
            after: 0,
            action: '卖出',
          },
          {
            symbol: 'SH688410',
            name: '山外山',
            price: 9.78,
            before: 0,
            after: 11.38,
            action: '买入',
          },
        ],
      },
    }))
  }
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (!path.includes('/social-monitor'))
      return route.fulfill({ status: 503, json: { detail: '仅模拟监控模块' } })
    expect(request.headers().cookie).toBeUndefined()
    if (path.endsWith('/posts')) return route.fulfill({ json: { total: 0, page: 1, items: [] } })
    if (path.endsWith('/cube-rebalances')) {
      reads.push(url.search)
      const filter = Number(url.searchParams.get('cube_id'))
      const currentPage = Number(url.searchParams.get('page') || 1)
      const rows = status.cubes.filter((cube) => !filter || cube.id === filter).flatMap(records)
      return route.fulfill({
        json: {
          total: rows.length,
          page: currentPage,
          items: rows.slice((currentPage - 1) * 20, currentPage * 20),
        },
      })
    }
    if (path.endsWith('/cubes') && request.method() === 'POST') {
      const body = request.postDataJSON()
      expect(Object.keys(body).sort()).toEqual(['name', 'url'])
      status.cubes.push({
        id: nextId++,
        symbol: body.url,
        name: body.name || body.url,
        cube_name: '',
        owner_name: '',
        enabled: 1,
        initialized: 1,
        last_check: '2026-10-25T10:28:00+08:00',
        last_success_at: '2026-10-25T10:28:00+08:00',
        last_error: null,
        scan_page: 1,
      })
    }
    const cubeMatch = path.match(/\/cubes\/(\d+)(\/delete)?$/)
    if (cubeMatch && request.method() === 'POST') {
      const id = Number(cubeMatch[1])
      if (cubeMatch[2]) {
        deleteRequests++
        status.cubes = status.cubes.filter((cube) => cube.id !== id)
      } else
        status.cubes.find((cube) => cube.id === id)!.enabled = Number(
          request.postDataJSON().enabled,
        )
    }
    if (path.endsWith('/config')) status.enabled = Boolean(request.postDataJSON().enabled)
    if (path.endsWith('/platforms/xueqiu/resume')) status.platforms.xueqiu.paused = false
    return route.fulfill({ json: status })
  })

  await page.goto('/social-monitor')
  await expect(page.getByRole('tab')).toHaveCount(4)
  const cubeCount = page
    .getByText('已启用组合 / 全部', { exact: true })
    .locator('..')
    .locator('strong')
  const authorCount = page
    .getByText('已启用博主 / 全部', { exact: true })
    .locator('..')
    .locator('strong')
  async function expectCounts(cubes: string, authors = '0 / 0') {
    await expect(cubeCount).toHaveText(cubes)
    await expect(authorCount).toHaveText(authors)
  }
  await expectCounts('0 / 0')
  expect(reads).toHaveLength(0)
  await page.getByRole('tab', { name: /组合调仓/ }).click()
  const panel = page.getByRole('tabpanel', { name: /组合调仓/ })
  await expect(panel.getByText('北京时间 周一至周五 09:00～15:00', { exact: true })).toBeVisible()
  await expect(panel.getByText('当前为非监控时段，下一个监控窗口按队列补齐新调仓')).toBeVisible()
  await expect(panel.getByRole('heading', { name: '添加要关注的组合' })).toBeVisible()
  await expect(panel.getByLabel('组合链接或代码')).toHaveValue('')
  await panel.getByLabel('组合链接或代码').fill('ZH679918')
  await panel.getByLabel('组合备注').fill('火种测试组合')
  await panel.getByRole('button', { name: '添加组合', exact: true }).click()
  await expect(panel.locator('.social-cube-card')).toHaveCount(1)
  await expectCounts('1 / 1')
  const firstCube = panel.locator('.social-cube-card').first()
  await expect(firstCube.locator('strong')).toHaveText('火种测试组合')
  await expect(firstCube.getByText('主理人：待获取', { exact: true })).toBeVisible()
  await expect(panel.getByRole('status')).toContainText('首次建立历史基线')
  status.cubes[0].cube_name = '火种燎原-A股'
  status.cubes[0].owner_name = '火蚁投资'
  await panel.getByRole('button', { name: '刷新调仓记录' }).click()
  await expect(firstCube.locator('strong')).toHaveText('火种燎原-A股')
  await expect(firstCube.getByText('主理人：火蚁投资', { exact: true })).toBeVisible()
  await expect(firstCube.getByText('备注：火种测试组合', { exact: true })).toBeVisible()
  await expect(panel.getByLabel('组合链接或代码')).toHaveValue('')
  await page.getByRole('button', { name: '启动监控', exact: true }).click()
  await expect(page.getByText('组合休息中', { exact: true })).toBeVisible()
  await expect(panel.locator('.social-cube-card')).toContainText('非监控时段')
  await expectCounts('1 / 1')

  await panel.getByLabel('组合链接或代码').fill('ZH000002')
  await panel.getByLabel('组合备注').fill('第二组合')
  await panel.getByRole('button', { name: '添加组合', exact: true }).click()
  await expect(panel.locator('.social-cube-card')).toHaveCount(2)
  await expectCounts('2 / 2')
  await expect(panel.locator('.social-cube-card').last()).toContainText('第二组合')
  await expect(panel.locator('.social-cube-card').last()).toContainText('主理人：待获取')
  await expect(
    panel.getByLabel('按组合筛选').getByRole('option', { name: '火种燎原-A股' }),
  ).toHaveCount(1)
  await panel.getByLabel('按组合筛选').selectOption('1')
  await expect(panel.getByText('共 21 条 · 北京时间 · 首次历史记录不推送')).toBeVisible()
  await expect(panel.locator('.social-rebalance')).toHaveCount(20)
  const first = panel.locator('.social-rebalance').first()
  await expect(first.locator('header strong')).toHaveText('火种燎原-A股')
  await expect(first.getByText('主理人：火蚁投资', { exact: true })).toBeVisible()
  await expect(first.getByText('备注：火种测试组合', { exact: true })).toBeVisible()
  await expect(first).toContainText('调仓成功')
  await expect(first).toContainText('已推送')
  await expect(first).toContainText('11.40% → 0.00%')
  await expect(first).toContainText('0.00% → 11.38%')
  await expect(first).toContainText('2026/10/25 10:25:32')
  await expect(first).toContainText('组合记录价：26.53')
  await expect(first).toContainText('<script>不会执行</script>')
  await expect(first.getByRole('link', { name: '查看组合' })).toHaveAttribute(
    'href',
    'https://www.xueqiu.com/P/ZH679918',
  )
  await panel.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(panel.locator('.social-rebalance')).toHaveCount(1)
  await expect(panel.getByText('第二页调仓', { exact: true })).toBeVisible()
  await panel.getByLabel('按组合筛选').selectOption('2')
  await expect(panel.getByText('历史记录', { exact: true })).toBeVisible()
  await expect(panel.getByText('第 1 / 1 页')).toBeVisible()
  expect(reads.some((query) => query.includes('cube_id=2'))).toBe(true)

  await panel.getByRole('button', { name: '暂停组合 ZH679918', exact: true }).click()
  await expect(panel.locator('.social-cube-card').first()).toContainText('该组合已手动暂停')
  await expectCounts('1 / 2')
  await panel.getByRole('button', { name: '恢复组合 ZH679918', exact: true }).click()
  await expectCounts('2 / 2')
  status.platforms.xueqiu = { paused: true, reason: '平台要求人工验证', alert_sent_at: null }
  await panel.getByRole('button', { name: '刷新调仓记录' }).click()
  await expect(panel.locator('.social-cube-card').first()).toContainText('平台要求人工验证')
  await expect(page.getByText('等待人工处理', { exact: true })).toBeVisible()
  await expectCounts('2 / 2')
  await page.getByRole('button', { name: '人工处理完成，恢复雪球' }).click()
  await expect(panel.locator('.social-cube-card').first()).not.toContainText('平台要求人工验证')

  page.once('dialog', (dialog) => void dialog.dismiss())
  await panel.getByRole('button', { name: '删除组合 ZH000002', exact: true }).click()
  expect(deleteRequests).toBe(0)
  await expectCounts('2 / 2')
  page.once('dialog', (dialog) => void dialog.accept())
  await panel.getByRole('button', { name: '删除组合 ZH000002', exact: true }).click()
  await expect(panel.locator('.social-cube-card')).toHaveCount(1)
  await expect(panel.getByLabel('按组合筛选')).toHaveValue('')
  await expect(panel.locator('.social-rebalance')).toHaveCount(20)
  await expectCounts('1 / 1')
  expect(deleteRequests).toBe(1)
  status.authors = [
    {
      id: 1,
      platform: 'weibo',
      uid: '101',
      name: '启用博主',
      enabled: 1,
      last_check: null,
      last_error: null,
    },
    {
      id: 2,
      platform: 'xueqiu',
      uid: '102',
      name: '暂停博主',
      enabled: 0,
      last_check: null,
      last_error: null,
    },
  ]
  await panel.getByRole('button', { name: '刷新调仓记录' }).click()
  await expectCounts('1 / 1', '1 / 2')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('tab', { name: /组合调仓/ }).focus()
  await page.keyboard.press('End')
  await expect(page.getByRole('tab', { name: '通知设置' })).toBeFocused()
  await page.getByRole('tab', { name: /组合调仓/ }).click()
  await panel.getByLabel('按组合筛选').selectOption('1')
  await panel.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(panel.getByText('第二页调仓', { exact: true })).toBeVisible()
  await page.evaluate(() => {
    ;(document.activeElement as HTMLElement)?.blur()
    window.scrollTo(0, 0)
  })
  await page.screenshot({ path: testInfo.outputPath('social-cubes.png'), fullPage: true })
  page.once('dialog', (dialog) => void dialog.accept())
  await panel.getByRole('button', { name: '删除组合 ZH679918', exact: true }).click()
  await expect(panel.locator('.social-cube-card')).toHaveCount(0)
  await expectCounts('0 / 0', '1 / 2')
  expect(errors).toEqual([])
})
