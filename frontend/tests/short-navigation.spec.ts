import { expect, test } from '@playwright/test'

test('five short-term links share layout across routes and history', async ({ page }, info) => {
  await page.route('**/api/v1/**', (r) =>
    r.fulfill({
      json: { enabled: false, modules: [], dates: [], counts: {}, trade_date: null, items: [] },
    }),
  )
  await page.route('**/validation/access', (r) =>
    r.fulfill({ json: { mode: 'pair', authenticated: false } }),
  )
  const names = ['补票龙', '红悬停龙', '蓄力龙', '超跌后止跌反弹', '胜率验证']
  const urls = [
    '/short-term?method=bukou',
    '/short-term?method=redhover',
    '/short-term?method=xuli',
    '/short-term?method=oversold',
    '/short-term/validation',
  ]
  await page.goto(urls[0])
  const nav = page.getByRole('navigation', { name: '短线选股战法' })
  const shape = () =>
    nav.locator('a').evaluateAll((links) =>
      links.map((link) => {
        const style = getComputedStyle(link)
        const text = getComputedStyle(link.querySelector('strong')!)
        return {
          width: Math.round(link.getBoundingClientRect().width * 100) / 100,
          height: Math.round(link.getBoundingClientRect().height * 100) / 100,
          padding: style.padding,
          gap: style.gap,
          font: text.fontSize,
        }
      }),
    )
  await expect(nav.locator('a')).toHaveCount(5)
  const initial = await shape()
  const screenshotDir = process.env.NAV_SCREENSHOT_DIR || info.outputPath('navigation')
  await nav.screenshot({
    path: `${screenshotDir}/${process.env.CAPTURE_NAV || 'after'}-${info.project.name}-strategy.png`,
  })
  for (let i = 0; i < names.length; i++) {
    await nav.getByRole('link', { name: names[i], exact: true }).click()
    await expect(page).toHaveURL(new RegExp(urls[i].replace('?', '\\?') + '$'))
    await expect(nav.locator('[aria-current="page"]')).toHaveAttribute('aria-label', names[i])
    await expect(nav.locator('a')).toHaveCount(5)
    if (!process.env.CAPTURE_NAV) expect(await shape()).toEqual(initial)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }
  await nav.screenshot({
    path: `${screenshotDir}/${process.env.CAPTURE_NAV || 'after'}-${info.project.name}-validation.png`,
  })
  await page.reload()
  await expect(nav.getByRole('link', { name: '胜率验证', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.goBack()
  await expect(nav.getByRole('link', { name: names[3], exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.goForward()
  await expect(nav.getByRole('link', { name: names[4], exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
})
