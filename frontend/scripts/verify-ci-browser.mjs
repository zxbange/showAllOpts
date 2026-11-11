// Fail closed before E2E when the lockfile and official browser image diverge.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

const require = createRequire(import.meta.url)
const packages = ['@playwright/test', 'playwright', 'playwright-core']

export function validateVersions(lock, installed, image, env) {
  const version = lock.packages?.['node_modules/@playwright/test']?.version
  assert.match(version ?? '', /^\d+\.\d+\.\d+$/)
  for (const name of packages) {
    assert.equal(lock.packages?.[`node_modules/${name}`]?.version, version, `${name} lock mismatch`)
    assert.equal(installed[name], version, `${name} install mismatch`)
  }
  assert.equal(image.driverVersion, version, 'Official image version must match the lockfile')
  assert.equal(image.dockerImageName, `mcr.microsoft.com/playwright:v${version}-noble`)
  assert.equal(env.PLAYWRIGHT_BROWSERS_PATH, '/ms-playwright')
  assert.equal(env.PLAYWRIGHT_BROWSER, 'chromium')
  assert.ok(!env.PLAYWRIGHT_EXECUTABLE_PATH, 'Do not substitute an untested browser')
  return version
}

async function main() {
  const lock = JSON.parse(readFileSync(new URL('../package-lock.json', import.meta.url), 'utf8'))
  const installed = Object.fromEntries(packages.map(name => [name, require(`${name}/package.json`).version]))
  const image = JSON.parse(readFileSync('/ms-playwright/.docker-info', 'utf8'))
  const version = validateVersions(lock, installed, image, process.env)
  const { chromium } = require('playwright')
  const browser = await chromium.launch({ headless: true, timeout: 30_000 })
  try {
    console.log(`Verified official Playwright ${version}; Chromium ${browser.version()} launched successfully.`)
  } finally {
    await browser.close()
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main()
}
