import { expect, test } from '@playwright/test'
import fs from 'node:fs'

test('real database: homepage, all industries, and yearly trend', async ({ page }, testInfo) => {
  test.setTimeout(60000)
  test.skip(process.env.E2E_REAL_DATA !== '1', 'Explicit opt-in for read-only live integration.')
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const screenshotDir = '../.runtime/screenshots'
  fs.mkdirSync(screenshotDir, { recursive: true })
  await page.goto('/')
  await expect(page.locator('.research-card')).toHaveCount(6)
  await page.screenshot({
    path: `${screenshotDir}/home-${testInfo.project.name}.png`,
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('link', { name: '进入行业动量' }).click()
  await expect(page.locator('.heat-cell').first()).toBeVisible({ timeout: 20000 })
  const scores = await page.locator('.cell-value').allTextContents()
  expect(scores.length).toBeGreaterThan(10)
  const values = scores.map(Number)
  expect(values).toEqual([...values].sort((a, b) => b - a))
  await page.waitForTimeout(700)
  await page.screenshot({
    path: `${screenshotDir}/heatmap-${testInfo.project.name}.png`,
    fullPage: true,
    animations: 'disabled',
  })
  await page.locator('.heat-cell').first().click()
  await expect(page.locator('.trend-chart canvas')).toBeVisible({ timeout: 20000 })
  await page.waitForTimeout(900)
  await page.screenshot({
    path: `${screenshotDir}/trend-${testInfo.project.name}.png`,
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(errors).toEqual([])
})
