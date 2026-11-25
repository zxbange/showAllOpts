import { apiFetch } from './http'

export interface DividendCollectionStatus {
  run_id?: string | null
  stage?: string
  failed_stage?: string | null
  processed?: number | null
  total?: number | null
  stage_started_at?: string
  last_attempt_at?: string
  running: boolean
  available: boolean
  unavailable_reason?: string | null
  error?: string | null
  interrupted?: boolean
  publication_pending?: boolean
  last_success_at?: string
}

export class DividendRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

async function request<T>(path: string, signal?: AbortSignal, method = 'GET'): Promise<T> {
  const response = await apiFetch('/api/v1/dividend-selection/' + path, { method, signal })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new DividendRequestError(
      typeof body?.detail === 'string' ? body.detail : `请求失败（${response.status}）`,
      response.status,
    )
  }
  return response.json() as Promise<T>
}
export const getDividendStatus = (signal?: AbortSignal) =>
  request<DividendCollectionStatus>('status', signal)
export const updateDividend = (action: 'refresh' | 'cancel', signal?: AbortSignal) =>
  request<{ started?: boolean; cancelled?: boolean }>(action, signal, 'POST')
