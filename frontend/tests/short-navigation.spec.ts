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

test('validation keeps counts and selected snapshot across routes and refresh', async ({
  page,
}) => {
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { enabled: false, modules: [] } }))
  await page.route('**/validation/access', (r) =>
    r.fulfill({ json: { mode: 'pair', authenticated: false } }),
  )
  await page.route('**/short-term/latest?*', (r) => {
    const query = new URL(r.request().url()).searchParams
    const old = query.get('day') === '2026-09-29'
    return r.fulfill({
      json: {
        trade_date: old ? '2026-09-29' : '2026-09-30',
        dates: ['2026-09-30', '2026-09-29'],
        counts: { bukou: old ? 17 : 7, redhover: 0, xuli: 3, oversold: 2 },
        method: query.get('method'),
        meta: {},
        total: 0,
        offset: 0,
        limit: 6,
        items: [],
      },
    })
  })
  const nav = page.getByRole('navigation', { name: '短线选股战法' })
  const numbers = nav.locator('.module-card-count b')
  await page.goto('/short-term?method=bukou')
  await expect(numbers).toHaveText(['7', '0', '3', '2'])
  await page.getByLabel('短线选股日期').selectOption('2026-09-29')
  await expect(numbers).toHaveText(['17', '0', '3', '2'])
  await nav.getByRole('link', { name: '胜率验证', exact: true }).click()
  await expect(page).toHaveURL(/validation\?day=2026-09-29$/)
  await expect(page.getByRole('heading', { name: '短线胜率验证', exact: true })).toBeVisible()
  await expect(numbers).toHaveText(['17', '0', '3', '2'])
  await page.screenshot({ path: test.info().outputPath('counts-validation.png'), fullPage: true, animations: 'disabled' })
  await page.reload()
  await expect(numbers).toHaveText(['17', '0', '3', '2'])
  await nav.getByRole('link', { name: '红悬停龙', exact: true }).click()
  await expect(page.getByLabel('短线选股日期')).toHaveValue('2026-09-29')
  await expect(numbers).toHaveText(['17', '0', '3', '2'])
  await page.goBack()
  await expect(numbers).toHaveText(['17', '0', '3', '2'])
  await page.goForward()
  await expect(numbers).toHaveText(['17', '0', '3', '2'])
  await page.goto('/short-term/validation')
  await expect(numbers).toHaveText(['7', '0', '3', '2'])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('counts distinguish loading, error, no snapshot and missing strategy', async ({ page }) => {
  await page.route('**/api/v1/**', (r) => r.fulfill({ json: { enabled: false, modules: [] } }))
  await page.route('**/validation/access', (r) =>
    r.fulfill({ json: { mode: 'pair', authenticated: false } }),
  )
  let release!: () => void
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  let mode = 'loading'
  await page.route('**/short-term/latest?*', async (r) => {
    if (mode === 'loading') {
      await held
      return r.fulfill({ status: 503, json: { detail: '模拟读取失败' } })
    }
    return r.fulfill({
      json: {
        trade_date: mode === 'empty' ? null : '2026-09-30',
        counts:
          mode === 'empty'
            ? { bukou: 0, redhover: 0, xuli: 0, oversold: 0 }
            : { bukou: 4, redhover: 0, xuli: 1 },
        dates: [],
        items: [],
        total: 0,
        meta: {},
      },
    })
  })
  await page.goto('/short-term/validation')
  const nav = page.getByRole('navigation', { name: '短线选股战法' })
  const numbers = nav.locator('.module-card-count b')
  await expect(nav.getByTitle('数量加载中')).toHaveCount(4)
  await expect(numbers).toHaveText(['—', '—', '—', '—'])
  await expect(page.getByLabel('一次性配对码')).toBeVisible()
  release()
  await expect(nav.getByTitle('数量加载失败')).toHaveCount(4)
  await expect(numbers).toHaveText(['—', '—', '—', '—'])
  mode = 'empty'
  await page.getByRole('button', { name: '重试数量' }).click()
  await expect(nav.getByTitle('暂无选股快照')).toHaveCount(4)
  await expect(numbers).toHaveText(['—', '—', '—', '—'])
  mode = 'partial'
  await page.evaluate(() => window.dispatchEvent(new Event('data-cache-updated')))
  await expect(numbers).toHaveText(['4', '0', '1', '—'])
  await expect(nav.getByTitle('此快照尚无该策略数量')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '重试数量' })).toHaveCount(0)
})
