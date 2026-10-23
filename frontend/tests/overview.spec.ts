import { expect, test } from '@playwright/test'

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
  await expect(page.locator('.research-card')).toHaveCount(8)
  await expect(page.getByRole('link', { name: '进入动量观察', exact: true })).toHaveAttribute(
    'href',
    '/industry-momentum',
  )
  await expect(page.getByRole('link', { name: '进入抱团拥挤度', exact: true })).toHaveAttribute(
    'href',
    '/market-crowding',
  )
  await expect(page.getByRole('link', { name: '进入微盘观察', exact: true })).toHaveAttribute(
    'href',
    '/microcap-temperature',
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
  await expect(page.locator('.research-card')).toHaveCount(8)
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
  await page.getByRole('link', { name: '进入抱团拥挤度', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/market-crowding$/)
  await expect(page.getByRole('alert')).toContainText('数据库暂不可用')
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
    ['抱团拥挤度', 'market-crowding'],
    ['微盘观察', 'microcap-temperature'],
    ['ETF份额仪表盘', 'etf-shares'],
    ['高股息选股', 'dividend-selection'],
    ['业绩选股', 'high-growth'],
  ]) {
    await navigation.getByRole('link', { name, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(path + '$'))
    await expect(page.getByRole('heading', { level: 1 })).toContainText(
      (
        { 动量观察: '行业动量', 微盘观察: '微盘温度计', 业绩选股: '高增长' } as Record<
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
  await expect(page.locator('.research-card')).toHaveCount(8)
})

test('group tabs preserve direct links, active navigation and mobile layout', async ({ page }) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '测试无数据' } }),
  )
  for (const [group, first, second, label] of [
    ['动量观察', 'industry-momentum', 'etf-momentum', 'ETF动量热度'],
    ['微盘观察', 'microcap-temperature', 'microcap-breadth', '微盘新高新低'],
    ['业绩选股', 'high-growth', 'earnings-gap', '净利润断层'],
  ]) {
    await page.goto('/' + first)
    const tabs = page.getByRole('navigation', { name: group + '模块切换' })
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
