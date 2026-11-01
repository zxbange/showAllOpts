import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { validationRequest } from './validation'
import { setCsrf } from './http'

beforeEach(() => {
  vi.stubGlobal('window', {
    location: { origin: 'https://site.example', href: 'https://site.example/' },
    dispatchEvent: vi.fn(),
  })
  setCsrf('page-only-csrf')
})
afterEach(() => vi.unstubAllGlobals())
it('uses session CSRF, no-store and forbids credential redirects', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  vi.stubGlobal('fetch', fetch)
  await validationRequest({ csrf: 'page-only-csrf' }, '/sources/test', {
    password: 'ephemeral-password',
  })
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/short-term/validation/sources/test',
    expect.objectContaining({
      cache: 'no-store',
      redirect: 'error',
      headers: expect.any(Headers),
    }),
  )
  const headers = fetch.mock.calls[0][1].headers as Headers
  expect(headers.get('Authorization')).toBeNull()
  expect(headers.get('X-CSRF-Token')).toBe('page-only-csrf')
})
it('does not stringify malformed server details containing secret inputs', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: [{ input: 'secret' }] }) }),
  )
  await expect(validationRequest({ csrf: 'page-only-csrf' }, '/sources/test', {})).rejects.toThrow(
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
      headers: expect.any(Headers),
    }),
  )
  const headers = fetch.mock.calls[0][1].headers as Headers
  expect(headers.get('X-CSRF-Token')).toBe('page-only-csrf')
  expect(headers.get('X-App-Client')).toBe('browser')
  expect(headers.get('X-Validation-CSRF')).toBeNull()
})
