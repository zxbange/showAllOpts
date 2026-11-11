import assert from 'node:assert/strict'
import test from 'node:test'
import { validateVersions } from './verify-ci-browser.mjs'

function fixture() {
  const names = ['@playwright/test', 'playwright', 'playwright-core']
  return [
    { packages: Object.fromEntries(names.map(name => [`node_modules/${name}`, { version: '1.64.1' }])) },
    Object.fromEntries(names.map(name => [name, '1.64.1'])),
    { driverVersion: '1.64.1', dockerImageName: 'mcr.microsoft.com/playwright:v1.64.1-noble' },
    { PLAYWRIGHT_BROWSERS_PATH: '/ms-playwright', PLAYWRIGHT_BROWSER: 'chromium' },
  ]
}

test('matching official image, lockfile and installed packages pass', () => {
  assert.equal(validateVersions(...fixture()), '1.64.1')
})

for (const [label, change] of [
  ['missing lock version', args => delete args[0].packages['node_modules/@playwright/test']],
  ['lock mismatch', args => args[0].packages['node_modules/playwright-core'].version = '1.64.0'],
  ['install mismatch', args => args[1].playwright = '1.64.0'],
  ['image mismatch', args => args[2].driverVersion = '1.64.0'],
  ['unapproved image name', args => args[2].dockerImageName = 'other/playwright:v1.64.1'],
  ['browser cache override', args => args[3].PLAYWRIGHT_BROWSERS_PATH = '/tmp/browser'],
  ['browser selection override', args => args[3].PLAYWRIGHT_BROWSER = 'chrome'],
  ['executable override', args => args[3].PLAYWRIGHT_EXECUTABLE_PATH = '/usr/bin/chromium'],
]) {
  test(`reject ${label}`, () => {
    const args = fixture()
    change(args)
    assert.throws(() => validateVersions(...args))
  })
}
