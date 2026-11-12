import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { SystemRuntimeConfiguration } from './SystemRuntimeConfiguration'
const value = {
  logging: { level: 'INFO', retention_days: 30 },
  database_sync: { read_timeout_seconds: 600, attempts: 3 },
  sources: {
    asharehub: { request_timeout_seconds: 60 },
    eastmoney: { request_timeout_seconds: 60 },
  },
}
describe('runtime settings', () => {
  it('shows bounded numeric controls and never invents source health', () => {
    const html = renderToStaticMarkup(
      createElement(SystemRuntimeConfiguration, {
        value,
        onChange: vi.fn(),
        statuses: [
          {
            id: 'asharehub',
            credential_required: true,
            credential_set: true,
            health: 'not_checked',
          },
        ],
      }),
    )
    expect(html).toContain('日志级别')
    expect(html).toContain('尝试次数（含首次）')
    expect(html).toContain('max="600"')
    expect(html).toContain('max="3600"')
    expect(html).toContain('max="3650"')
    expect(html).toContain('尚未检测可用性')
    expect(html).not.toContain('在线')
  })
  it('shows unconfigured secret source separately from public feeds', () => {
    const html = renderToStaticMarkup(
      createElement(SystemRuntimeConfiguration, {
        value,
        onChange: vi.fn(),
        statuses: [{ id: 'asharehub', credential_required: true, credential_set: false }],
      }),
    )
    expect(html).toContain('未配置')
    expect(html).toContain('公开数据源')
  })
})
