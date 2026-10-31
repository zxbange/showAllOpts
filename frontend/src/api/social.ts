/** Shared client for author and portfolio monitoring. */
export async function socialRequest<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/v1/social-monitor${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok)
    throw new Error(
      typeof data?.detail === 'string' ? data.detail : `请求失败（${response.status}）`,
    )
  return data as T
}
