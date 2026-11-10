import { expect, test } from './fixtures'

test('overview directory is independent of database, searchable and categorized', async ({
  page,
}, testInfo) => {
  const requests: string[] = []
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/**', (route) => {
    requests.push(route.request().url())
    return route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('全部研究模块')
  await expect(page.locator('.research-card')).toHaveCount(7)
  await expect(page.getByRole('link', { name: '进入动量观察', exact: true })).toHaveAttribute(
    'href',
    '/industry-momentum',
  )
  await expect(page.getByRole('link', { name: '进入市场情绪', exact: true })).toHaveAttribute(
    'href',
    '/etf-shares',
  )
  await expect(page.getByRole('link', { name: '进入微盘观察', exact: true })).toHaveAttribute(
    'href',
    '/microcap-temperature',
  )
  await expect(page.getByRole('link', { name: '进入业绩选股', exact: true })).toHaveAttribute(
    'href',
    '/cash-cow',
  )
  await page.getByRole('button', { name: '风格观察 1', exact: true }).click()
  await expect(page.locator('.research-card')).toHaveCount(1)
  await expect(page.getByRole('link', { name: '进入微盘观察', exact: true })).toBeVisible()
  const search = page.getByRole('textbox', { name: '搜索研究模块' })
  await search.fill('行业')
  await expect(page.getByRole('heading', { name: '没有找到匹配的模块' })).toBeVisible()
  await page.getByRole('button', { name: '查看全部模块' }).click()
  await search.fill('EMA')
  await expect(page.locator('.research-card')).toHaveCount(1)
  await expect(page.locator('.research-card')).toContainText('抱团拥挤度')
  await page.getByRole('button', { name: '清空模块搜索' }).click()
  await expect(page.locator('.research-card')).toHaveCount(7)
  // Mobile browsers may keep the focused search field at the top after clearing.
  await page.getByRole('heading', { name: /全部研究模块/ }).scrollIntoViewIfNeeded()
  await expect(page.getByRole('heading', { name: /全部研究模块/ })).toBeInViewport()
  expect(requests.filter((url) => !url.endsWith('/cache/status'))).toEqual([])
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.evaluate(() => {
    ;(document.activeElement as HTMLElement)?.blur()
    window.scrollTo(0, 0)
  })
  await page.screenshot({
    path: testInfo.outputPath('overview.png'),
    fullPage: true,
    animations: 'disabled',
  })
  // A failed database must not prevent keyboard access to any registered module.
  await page.getByRole('link', { name: '进入市场情绪', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/etf-shares$/)
  await expect(page.locator('.state-panel[role="alert"]')).toContainText('数据库暂不可用')
})

test('all module routes remain reachable from navigation', async ({ page }) => {
  test.setTimeout(60000)
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '数据库暂不可用' } }),
  )
  await page.goto('/')
  const navigation = page.getByRole('navigation', { name: '主导航' })
  for (const [name, path] of [
    ['动量观察', 'industry-momentum'],
    ['市场情绪', 'etf-shares'],
    ['微盘观察', 'microcap-temperature'],
    ['业绩选股', 'cash-cow'],
  ]) {
    await navigation.getByRole('link', { name, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(path + '$'))
    await expect(page.getByRole('heading', { level: 1 })).toContainText(
      (
        { 动量观察: '行业动量', 微盘观察: '微盘温度计', 业绩选股: '奶牛股' } as Record<
          string,
          string
        >
      )[name] ?? name,
      { timeout: 15000 },
    )
    await expect(navigation.getByRole('link', { name, exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    )
  }
  await navigation.getByRole('link', { name: '总览', exact: true }).click()
  await expect(page.locator('.research-card')).toHaveCount(7)
})

test('group tabs preserve direct links, active navigation and mobile layout', async ({ page }) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '测试无数据' } }),
  )
  for (const [group, first, second, label] of [
    ['动量观察', 'industry-momentum', 'etf-momentum', 'ETF动量热度'],
    ['市场情绪', 'etf-shares', 'market-crowding', '抱团拥挤度'],
    ['微盘观察', 'microcap-temperature', 'microcap-breadth', '微盘新高新低'],
    ['业绩选股', 'cash-cow', 'earnings-gap', '净利润断层'],
    ['业绩选股', 'cash-cow', 'high-growth', '高增长'],
    ['业绩选股', 'high-growth', 'cash-cow', '奶牛股'],
  ]) {
    await page.goto('/' + first)
    const tabs = page.getByRole('navigation', { name: group + '模块切换' })
    await expect(tabs).toBeVisible()
    const heading = await page.locator('.page-header .page-heading').boundingBox()
    const cards = await tabs.boundingBox()
    expect(cards!.y + cards!.height).toBeLessThanOrEqual(heading!.y)
    await expect(tabs.locator('.module-card')).toHaveCount(
      group === '市场情绪' ? 5 : group === '业绩选股' ? 6 : 2,
    )
    await tabs.getByRole('link', { name: label, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(second + '$'))
    await expect(tabs.getByRole('link', { name: label, exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    )
    await expect(
      page
        .getByRole('navigation', { name: '主导航' })
        .getByRole('link', { name: group, exact: true }),
    ).toHaveAttribute('aria-current', 'page')
    await page.reload()
    await expect(tabs).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }
})

test('shared module cards stay available while loading and after failures', async ({ page }) => {
  test.setTimeout(60000)
  for (const [path, count] of [
    ['etf-shares', 5],
    ['dividend-selection', 6],
  ] as const) {
    let release!: () => void
    const response = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route('**/api/v1/**', async (route) => {
      await response
      await route.fulfill({ status: 503, json: { detail: '测试加载失败' } })
    })
    await page.goto('/' + path)
    const cards = page.locator('.page-header .module-card')
    try {
      await expect(cards).toHaveCount(count)
      await expect(cards.first()).toBeVisible()
      await expect(page.locator('.section-eyebrow, .eyebrow, .research-english')).toHaveCount(0)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      )
    } finally {
      release()
    }
    await expect(page.locator('.state-panel[role=alert]')).toContainText('测试加载失败')
    await expect(cards).toHaveCount(count)
    await page.unroute('**/api/v1/**')
  }
})

test('navigation prioritizes market views and removes workspace label', async ({
  page,
}, testInfo) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线测试' } }),
  )
  await page.goto('/')
  const navigation = page.getByRole('navigation', { name: '主导航' })
  const names = await navigation
    .getByRole('link')
    .evaluateAll((links) => links.map((link) => link.getAttribute('aria-label')))
  expect(names).toEqual([
    '总览',
    '市场情绪',
    '动量观察',
    '微盘观察',
    '大宗商品',
    '个股机构持仓',
    '业绩选股',
    '博主动态监控',
    '个人持仓',
  ])
  await expect(page.getByText('工作空间', { exact: true })).toHaveCount(0)
  if (testInfo.project.name === 'desktop') {
    const brand = page.getByRole('link', { name: '发奋墙 首页' })
    await expect(brand).toBeVisible()
    expect(await brand.evaluate((el) => getComputedStyle(el).borderBottomWidth)).toBe('1px')
  }
  await page.screenshot({
    path: testInfo.outputPath('navigation-order.png'),
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('compact bookmarks scroll and support keyboard without activating the wrong module', async ({
  page,
}, info) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线布局测试' } }),
  )
  await page.goto('/etf-shares')
  const nav = page.getByRole('navigation', { name: '市场情绪模块切换' })
  const first = nav.getByRole('link', { name: '国家队动态', exact: true })
  const last = nav.getByRole('link', { name: '两融担保比例', exact: true })
  await expect(nav.getByRole('link')).toHaveText([
    '国家队动态',
    '创红比',
    '抱团拥挤度',
    '市场热度',
    '两融担保比例',
  ])
  await first.focus()
  await page.keyboard.press('End')
  await expect(last).toBeFocused()
  await expect(page).toHaveURL(/etf-shares$/)
  const bounds = await nav.evaluate((el) => {
    const items = Array.from(el.children).map((child) => child.getBoundingClientRect())
    const rect = el.getBoundingClientRect()
    return {
      rows: new Set(items.map((item) => Math.round(item.bottom))).size,
      height: items[0].height,
      scrollable: el.scrollWidth > el.clientWidth,
      offset: el.scrollLeft,
      lastVisible: items.at(-1)!.right <= rect.right + 1,
      bodyOverflow: document.documentElement.scrollWidth > innerWidth,
    }
  })
  expect(bounds.rows).toBe(1)
  expect(bounds.height).toBeCloseTo(54, 2)
  expect(bounds.lastVisible).toBe(true)
  expect(bounds.bodyOverflow).toBe(false)
  if (info.project.name === 'mobile') {
    expect(bounds.scrollable).toBe(true)
    expect(bounds.offset).toBeGreaterThan(0)
    await nav.evaluate((el) => {
      el.scrollLeft = 0
    })
    await expect.poll(() => nav.evaluate((el) => el.scrollLeft)).toBe(0)
    await last.focus()
  }
  await page.keyboard.press('Home')
  await expect(first).toBeFocused()
  await page.keyboard.press('ArrowRight')
  await expect(nav.getByRole('link', { name: '创红比', exact: true })).toBeFocused()
  await page.keyboard.press('ArrowLeft')
  await expect(first).toBeFocused()
  await page.keyboard.press('ArrowLeft')
  await expect(last).toBeFocused()
  await expect(page).toHaveURL(/etf-shares$/)
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/margin-guarantee$/)
  await expect(last).toHaveAttribute('aria-current', 'page')
  await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
  await page.reload()
  await expect(last).toHaveAttribute('aria-current', 'page')
  await page.screenshot({ path: info.outputPath('bookmarks-market.png'), fullPage: true })
  await page.goBack()
  await expect(first).toHaveAttribute('aria-current', 'page')
  await page.goto('/etf-shares')
  await expect(nav.getByRole('link', { name: '国家队动态', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(
    page
      .getByRole('navigation', { name: '主导航' })
      .getByRole('link', { name: '市场情绪', exact: true }),
  ).toHaveAttribute('aria-current', 'page')
  await page.goto('/')
  for (const term of ['国家队动态', 'ETF份额', '流通市值']) {
    await page.getByRole('textbox', { name: '搜索研究模块' }).fill(term)
    await expect(page.locator('.research-card')).toHaveCount(1)
    await expect(page.getByRole('link', { name: '进入市场情绪', exact: true })).toHaveAttribute(
      'href',
      '/etf-shares',
    )
  }
  await expect(page.getByRole('link', { name: '进入国家队动态', exact: true })).toHaveCount(0)
})

test('page-local module bookmarks stay above titles and preserve ordinary controls', async ({
  page,
}, info) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线布局测试' } }),
  )
  for (const path of [
    '/dividend-selection',
    '/cash-cow',
    '/institution-ownership',
    '/commodities?instrument=cci',
    '/social-monitor',
  ]) {
    await page.goto(path)
    const nav = page.locator('main .module-card-switch').first()
    await expect(nav).toBeVisible()
    const box = await nav.boundingBox()
    const heading = await page.getByRole('heading', { level: 1 }).boundingBox()
    expect(box!.y + box!.height).toBeLessThanOrEqual(heading!.y)
    expect(
      await nav.evaluate(
        (el) =>
          new Set(
            Array.from(el.children).map((item) => Math.round(item.getBoundingClientRect().bottom)),
          ).size,
      ),
    ).toBe(1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({
      path: info.outputPath(`bookmarks-${path.split('?')[0].slice(1)}.png`),
      fullPage: true,
    })
  }
  const tabs = page.getByRole('tablist', { name: '博主监控分区' })
  await tabs.getByRole('tab').first().focus()
  await page.keyboard.press('End')
  await expect(tabs.getByRole('tab').last()).toHaveAttribute('aria-selected', 'true')
  await expect(tabs.getByRole('tab').last()).toBeFocused()
  await page.keyboard.press('Home')
  await expect(tabs.getByRole('tab').first()).toHaveAttribute('aria-selected', 'true')
})
