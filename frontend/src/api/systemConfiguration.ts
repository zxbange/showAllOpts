import type {
  RuntimeConfiguration,
  DataSourceStatus,
} from '../components/SystemRuntimeConfiguration'
import { apiFetch } from './http'
export type TaskConfiguration = {
  enabled: boolean
  telegram_notify?: boolean
  times?: string[]
  frequency?: string
  start?: string
  end?: string
  interval_seconds?: number
  table?: string
  min_interval_minutes?: number
  max_interval_minutes?: number
}
export type SystemConfiguration = {
  runtime?: RuntimeConfiguration
  data_sources?: DataSourceStatus[]
  revision: number
  database_revision: number
  timezone: string
  migration_warnings?: string[]
  database: {
    host: string
    port: number
    database: string
    username: string
    password_set?: boolean
    password?: string | null
  }
  schedules: Record<string, TaskConfiguration>
  integrations?: {
    asharehub: { token_set: boolean }
    telegram: { token_set: boolean; chat_id_set: boolean }
  }
}
export async function systemConfigurationRequest<T>(
  method = 'GET',
  body?: unknown,
  test = false,
  signal?: AbortSignal,
): Promise<T> {
  const response = await apiFetch('/api/v1/system-configuration' + (test ? '/test' : ''), {
    method,
    signal,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const result = await response.json().catch(() => null)
  if (!response.ok)
    throw new Error(typeof result?.detail === 'string' ? result.detail : '系统配置请求失败，请重试')
  return result as T
}
