export async function validationRequest<T>(key: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch('/api/v1/short-term/validation' + path, {
    method: body === undefined ? 'GET' : 'POST',
    cache: 'no-store',
    redirect: 'error',
    headers: {
      Authorization: 'Bearer ' + key,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data = await response.json()
  if (!response.ok)
    throw new Error(
      typeof data.detail === 'string' ? data.detail : '操作失败，请检查输入与管理配置',
    )
  return data as T
}
