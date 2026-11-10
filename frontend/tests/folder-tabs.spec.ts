import { test, expect } from './fixtures'

test('six performance modules preserve URLs and folder layers, contrast and hit targets', async ({
  page,
}, info) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({ status: 503, json: { detail: '离线导航测试' } }),
  )
  await page.goto('/dividend-selection')
  const nav = page.getByRole('navigation', { name: '业绩选股模块切换' })
  const names = [
    '奶牛股',
    '高增长',
    '净利润断层',
    '纯高股息',
    '高股息低估值（非银行业）',
    '高股息低估值（银行业）',
  ]
  await expect(nav.getByRole('link')).toHaveCount(6)
  expect(await nav.getByRole('link').allTextContents()).toEqual(names)
  await expect(nav.getByRole('link', { name: '纯高股息', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  const initialHeight = await nav.evaluate((el) => el.getBoundingClientRect().height)
  for (const [name, method] of [
    ['高股息低估值（非银行业）', 'value'],
    ['高股息低估值（银行业）', 'bank'],
    ['纯高股息', 'classic'],
  ]) {
    await nav.getByRole('link', { name, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`dividend-selection\\?method=${method}$`))
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
    await expect(nav.getByRole('link', { name, exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(await nav.evaluate((el) => Math.round(el.getBoundingClientRect().height))).toBeCloseTo(
      initialHeight,
      2,
    )
    await expect
      .poll(() =>
        nav.locator('.active').evaluate((el) => Math.round(el.getBoundingClientRect().height)),
      )
      .toBe(54)
    const result = await nav.evaluate((el) => {
      const luminance = (color: string) => {
        const values = color
          .match(/[\d.]+/g)!
          .slice(0, 3)
          .map(Number)
          .map((v) => {
            v /= 255
            return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
          })
        return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722
      }
      const tabs = Array.from(el.children) as HTMLElement[]
      const active = tabs.find((t) => t.classList.contains('active'))!
      const inactive = tabs.filter((t) => t !== active)
      return {
        activeHeight: Math.round(active.getBoundingClientRect().height),
        inactiveHeights: inactive.map((t) => Math.round(t.getBoundingClientRect().height)),
        topLayer: inactive.every(
          (t) => +getComputedStyle(active).zIndex > +getComputedStyle(t).zIndex,
        ),
        overlaps: tabs
          .slice(1)
          .map((t, i) =>
            Math.round(tabs[i].getBoundingClientRect().right - t.getBoundingClientRect().left),
          ),
        colors: tabs.map((t) => getComputedStyle(t, '::before').backgroundColor),
        tones: tabs.map((t) => t.dataset.tone),
        contrasts: tabs.map((t) => {
          const foreground = luminance(getComputedStyle(t).color),
            background = luminance(getComputedStyle(t, '::before').backgroundColor)
          return (
            (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
          )
        }),
      }
    })
    expect(result.activeHeight).toBe(54)
    expect(result.inactiveHeights).toEqual([44, 44, 44, 44, 44])
    expect(result.topLayer).toBe(true)
    expect(result.overlaps.every((v) => v === 12)).toBe(true)
    expect(result.tones).toEqual(['0', '1', '2', '3', '4', '0'])
    // Six tabs cycle five base tones. When the first/last tone is active,
    // its active shade differs from the other tab's inactive shade.
    expect(new Set(result.colors).size).toBe(names.indexOf(name) % 5 === 0 ? 6 : 5)
    expect(Math.min(...result.contrasts)).toBeGreaterThanOrEqual(4.5)
  }
  for (const link of await nav.getByRole('link').all()) {
    await link.focus()
    const unobstructed = await link.evaluate((el) => {
      const text = el.querySelector('strong')!.getBoundingClientRect()
      return [text.left + 2, (text.left + text.right) / 2, text.right - 2].every((x) =>
        el.contains(document.elementFromPoint(x, (text.top + text.bottom) / 2)),
      )
    })
    expect(unobstructed).toBe(true)
  }
  await page.keyboard.press('Home')
  await expect(nav.getByRole('link', { name: '奶牛股', exact: true })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/cash-cow$/)
  await page.reload()
  await expect(nav.getByRole('link', { name: '奶牛股', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.screenshot({ path: info.outputPath('performance-folders.png'), fullPage: true })
  await page.goBack()
  await expect(nav.getByRole('link', { name: '纯高股息', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.goForward()
  await expect(nav.getByRole('link', { name: '奶牛股', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.goto('/')
  await expect(page.getByRole('link', { name: '进入高股息选股', exact: true })).toHaveCount(0)
  await page.getByRole('textbox', { name: '搜索研究模块' }).fill('高股息')
  await expect(page.locator('.research-card')).toHaveCount(1)
  const entry = page.getByRole('link', { name: '进入业绩选股', exact: true })
  await expect(entry).toHaveAttribute('href', '/cash-cow')
  await entry.click()
  await expect(page).toHaveURL(/cash-cow$/)
  await expect(nav.getByRole('link').first()).toHaveText('奶牛股')
  await expect(nav.getByRole('link').first()).toHaveAttribute('aria-current', 'page')
  await nav.getByRole('link', { name: '高增长', exact: true }).click()
  await expect(page).toHaveURL(/high-growth$/)
  const mainEntry = page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '业绩选股', exact: true })
  for (let repeat = 0; repeat < 2; repeat += 1) {
    await mainEntry.click()
    await expect(page).toHaveURL(/cash-cow$/)
    await expect(nav.getByRole('link').first()).toHaveAttribute('aria-current', 'page')
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
  }
})
