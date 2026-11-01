let csrf = ''
export function setCsrf(value: string) {
  csrf = value
}

/** Cookies stay HttpOnly; only the anti-CSRF token is held in page memory. */
export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const url = new URL(
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
    window.location.href || window.location.origin + '/',
  )
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/'))
    return fetch(input, init)
  const sentCsrf = csrf
  const headers = new Headers(
    init.headers ?? (input instanceof Request ? input.headers : undefined),
  )
  const method = init.method ?? (input instanceof Request ? input.method : 'GET')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
    headers.set('X-App-Client', 'browser')
    if (csrf) headers.set('X-CSRF-Token', csrf)
  }
  const response = await fetch(input, {
    ...init,
    headers,
    credentials: 'same-origin',
    redirect: 'error',
  })
  if (
    response.status === 401 &&
    sentCsrf === csrf &&
    !['/api/auth/login', '/api/auth/session'].includes(url.pathname)
  ) {
    setCsrf('')
    window.dispatchEvent(new Event('account-session-expired'))
  }
  return response
}

export async function accountRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await apiFetch('/api/auth' + path, {
    method: body === undefined ? 'GET' : 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const result = await response.json().catch(() => null)
  if (!response.ok)
    throw new Error(typeof result?.detail === 'string' ? result.detail : '请求失败，请稍后重试')
  return result as T
}
