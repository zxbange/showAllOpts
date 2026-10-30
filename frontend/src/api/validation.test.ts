import { afterEach, expect, it, vi } from 'vitest'
import { validationRequest } from './validation'
afterEach(() => vi.unstubAllGlobals())
it('uses in-memory bearer auth, no-store and forbids credential redirects', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  vi.stubGlobal('fetch', fetch)
  await validationRequest('ephemeral-admin', '/sources/test', { password: 'ephemeral-password' })
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/short-term/validation/sources/test',
    expect.objectContaining({
      cache: 'no-store',
      redirect: 'error',
      headers: expect.objectContaining({ Authorization: 'Bearer ephemeral-admin' }),
    }),
  )
})
it('does not stringify malformed server details containing secret inputs', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: [{ input: 'secret' }] }) }),
  )
  await expect(validationRequest('key', '/sources/test', {})).rejects.toThrow(
    '操作失败，请检查输入与管理配置',
  )
})

it('uses same-origin session cookies and CSRF headers without bearer or persistent browser storage', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  vi.stubGlobal('window', { location: { origin: 'http://site.example:8081' } })
  vi.stubGlobal('fetch', fetch)
  await validationRequest({ csrf: 'page-only-csrf' }, '/jobs', { methods: ['bukou'] })
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/short-term/validation/jobs',
    expect.objectContaining({
      credentials: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      headers: {
        'X-Validation-Client': 'browser',
        'X-Validation-Origin': 'http://site.example:8081',
        'X-Validation-CSRF': 'page-only-csrf',
        'Content-Type': 'application/json',
      },
    }),
  )
})
