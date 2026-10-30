import { expect, test } from '@playwright/test'
import { mkdir } from 'node:fs/promises'

const summary = ['bukou', 'redhover', 'xuli', 'oversold'].flatMap((strategy, i) =>
  [1, 2, 3].map((h) => ({
    策略: strategy,
    后续交易日: h,
    触发样本: 240 + i * 37,
    股票数: 87 + i * 9,
    有效样本: 218 + i * 30,
    上涨次数: 132,
    持平次数: 4,
    下跌次数: 82,
    '胜率(%)': 60.55 + i * 2 - h,
    '平均收益(%)': 1.24 + i * 0.4,
    '中位收益(%)': 0.63,
    '最差收益(%)': -9.83,
    '最好收益(%)': 18.41,
    待观察: 8,
    其余不可统计: 14,
    停牌样本: 4,
    行情缺失: 10,
  })),
)
const windows = ['bukou', 'redhover', 'xuli', 'oversold'].map((strategy) => ({
  策略: strategy,
  触发样本: 240,
  完整三日样本: 210,
  '三日内任一收盘上涨率(%)': 71.24,
  '三日内最好收盘收益均值(%)': 3.21,
  '三日内最差收盘收益均值(%)': -1.23,
}))
for (const state of ['unpaired', 'configuration', 'running', 'completed'] as const) {
  test(`visual ${state}`, async ({ page }, info) => {
    await page.route('**/api/v1/**', (r) =>
      r.fulfill({ json: { enabled: false, modules: [], dates: [], counts: {} } }),
    )
    await page.route('**/api/v1/short-term/validation/**', (r) => {
      const path = new URL(r.request().url()).pathname
      if (path.endsWith('/access'))
        return r.fulfill({
          json: {
            mode: 'pair',
            authenticated: state !== 'unpaired',
            csrf_token: 'mock-csrf',
            https_required: false,
          },
        })
      if (path.endsWith('/sources')) return r.fulfill({ json: { sources: [] } })
      return r.fulfill({
        json: {
          jobs: ['running', 'completed'].includes(state)
            ? [
                {
                  id: 'visual-job',
                  methods: ['bukou', 'redhover', 'xuli', 'oversold'],
                  start: '2024-01-01',
                  end: '2026-09-30',
                  observed: '2026-10-09',
                  version: 'four-close-v1',
                  status: state,
                  stage: state === 'running' ? '计算历史信号' : '统计完成',
                  captured: 3240,
                  done: state === 'running' ? 1382 : 3240,
                  total: 3240,
                  updated_at: '2026-10-30T08:30:00Z',
                  error: null,
                  source: { id: 'a'.repeat(32), revision: 2 },
                  summary: state === 'completed' ? summary : [],
                  windows: state === 'completed' ? windows : [],
                },
              ]
            : [],
        },
      })
    })
    await page.goto('/short-term/validation')
    if (state === 'unpaired') await expect(page.getByLabel('一次性配对码')).toBeVisible()
    else if (state === 'configuration')
      await expect(page.getByLabel('数据库地址', { exact: true })).toBeVisible()
    else await expect(page.getByRole('heading', { name: /补票龙.*—/ })).toBeVisible()
    if (state === 'configuration') {
      await page.getByLabel('数据库地址', { exact: true }).fill('research-db.example')
      await page.getByLabel('数据库名', { exact: true }).fill('research')
      await page.getByLabel('数据库用户名', { exact: true }).fill('readonly')
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const directory = process.env.VALIDATION_SCREENSHOTS
    if (directory) {
      await mkdir(directory, { recursive: true })
      await page.getByRole('heading', { level: 1 }).click()
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.screenshot({
        animations: 'disabled',
        path: `${directory}/${info.project.name}-${state}.png`,
        fullPage: true,
      })
    }
    if (state === 'configuration') {
      await page.getByLabel('数据库地址', { exact: true }).focus()
      await page.keyboard.press('Tab')
      await expect(page.getByLabel('数据库端口', { exact: true })).toBeFocused()
      await page.keyboard.press('Tab')
      await expect(page.getByLabel('数据库名', { exact: true })).toBeFocused()
      const advanced = page.locator('summary').filter({ hasText: '连接安全选项' })
      await advanced.focus()
      await page.keyboard.press('Enter')
      await page.getByLabel('数据库TLS').selectOption('required')
      await expect(page.getByLabel('数据库CA证书', { exact: true })).toBeVisible()
    }
    if (state === 'running') {
      await expect(page.getByRole('progressbar', { name: '冻结输入' })).toHaveAttribute(
        'value',
        '3240',
      )
      await expect(page.getByRole('progressbar', { name: '信号计算' })).toHaveAttribute(
        'value',
        '1382',
      )
      await expect(page.getByLabel('数据库地址', { exact: true })).not.toBeVisible()
      await page.locator('summary').filter({ hasText: '配置新的验证任务' }).click()
      await expect(page.getByLabel('数据库地址', { exact: true })).toBeVisible()
    }
    if (state === 'completed') {
      await page.getByRole('button', { name: 'T+2', exact: true }).focus()
      await page.keyboard.press('Enter')
      await expect(
        page
          .getByRole('region', { name: 'T+2策略对比' })
          .getByRole('row')
          .filter({ hasText: '补票龙' }),
      ).toContainText('58.55')
      await page.getByRole('button', { name: 'T+3', exact: true }).click()
      await expect(
        page
          .getByRole('region', { name: 'T+3策略对比' })
          .getByRole('row')
          .filter({ hasText: '补票龙' }),
      ).toContainText('57.55')
      const details = page.locator('summary').filter({ hasText: '完整统计明细' })
      await details.focus()
      await page.keyboard.press('Enter')
      await expect(page.getByRole('columnheader', { name: '行情缺失', exact: true })).toBeVisible()
      const table = page.getByRole('region', { name: '全部期限完整统计' })
      await table.focus()
      await page.keyboard.press('ArrowRight')
      await expect.poll(() => table.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0)
      await page.locator('summary').filter({ hasText: '三日窗口统计' }).click()
      await expect(page.getByRole('region', { name: '三日窗口统计' })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      )
    }
  })
}
