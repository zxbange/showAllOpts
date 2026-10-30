export type ValidationAuth = string | { csrf: string }

export async function validationRequest<T>(
  key: ValidationAuth,
  path: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch('/api/v1/short-term/validation' + path, {
    method: body === undefined ? 'GET' : 'POST',
    cache: 'no-store',
    redirect: 'error',
    credentials: 'same-origin',
    headers: {
      ...(typeof key === 'string'
        ? { Authorization: 'Bearer ' + key }
        : {
            'X-Validation-CSRF': key.csrf,
            'X-Validation-Client': 'browser',
            'X-Validation-Origin': window.location.origin,
          }),
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data = await response.json()
  if (!response.ok) {
    if (response.status === 401 && typeof key !== 'string')
      window.dispatchEvent(new Event('validation-session-expired'))
    throw new Error(
      typeof data.detail === 'string' ? data.detail : '操作失败，请检查输入与管理配置',
    )
  }
  return data as T
}
