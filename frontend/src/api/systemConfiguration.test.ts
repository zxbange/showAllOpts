import { afterEach, describe, expect, it, vi } from 'vitest'
import { systemConfigurationRequest } from './systemConfiguration'
vi.mock('./http', () => ({ apiFetch: vi.fn() }))
import { apiFetch } from './http'
afterEach(() => vi.clearAllMocks())
describe('central system configuration', () => {
  it('loads without credentials', async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response('{}'))
    await systemConfigurationRequest()
    expect(apiFetch).toHaveBeenCalledWith(
      '/api/v1/system-configuration',
      expect.objectContaining({ method: 'GET', cache: 'no-store' }),
    )
  })
  it('saves with PUT preserving null passwords', async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response('{}'))
    const body = { revision: 1, database: { password: null }, schedules: {} }
    await systemConfigurationRequest('PUT', body)
    expect(apiFetch).toHaveBeenCalledWith(
      '/api/v1/system-configuration',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify(body) }),
    )
  })
  it('reports a conflict', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: '配置已更新' }), { status: 409 }),
    )
    await expect(systemConfigurationRequest('PUT', {})).rejects.toThrow('配置已更新')
  })
  it('tests without saving', async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response('{}'))
    await systemConfigurationRequest('POST', {}, true)
    expect(apiFetch).toHaveBeenCalledWith(
      '/api/v1/system-configuration/test',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
