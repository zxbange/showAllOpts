import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiFetch, setCsrf } from './http'

beforeEach(() => {
  setCsrf('current-session')
  vi.stubGlobal('window', {
    location: { href: 'https://site.example/', origin: 'https://site.example' },
    dispatchEvent: vi.fn(),
  })
})
afterEach(() => vi.unstubAllGlobals())

it('sends cookies and CSRF only to the same-origin API', async () => {
  const fetcher = vi.fn().mockResolvedValue({ status: 200 })
  vi.stubGlobal('fetch', fetcher)
  await apiFetch('/api/v1/cache/refresh', { method: 'POST' })
  const options = fetcher.mock.calls[0][1]
  expect(options.credentials).toBe('same-origin')
  expect(options.redirect).toBe('error')
  expect(options.headers.get('X-CSRF-Token')).toBe('current-session')
  expect(options.headers.get('X-App-Client')).toBe('browser')
  await apiFetch('https://other.example/api/collect', { method: 'POST' })
  expect(fetcher.mock.calls[1][1]).toEqual({ method: 'POST' })
})

it('a revoked account session clears the page, including user management requests', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 401 }))
  await apiFetch('/api/auth/users')
  expect(window.dispatchEvent).toHaveBeenCalledOnce()
  expect((window.dispatchEvent as ReturnType<typeof vi.fn>).mock.calls[0][0].type).toBe(
    'account-session-expired',
  )
})

it('an old unauthorized response cannot log out a newly signed-in session', async () => {
  let complete!: (value: { status: number }) => void
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve
        }),
    ),
  )
  const pending = apiFetch('/api/v1/cache/status')
  setCsrf('new-session')
  complete({ status: 401 })
  await pending
  expect(window.dispatchEvent).not.toHaveBeenCalled()
})

it('wrong login credentials do not emit an expired-session event', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 401 }))
  await apiFetch('/api/auth/login', { method: 'POST' })
  expect(window.dispatchEvent).not.toHaveBeenCalled()
})
