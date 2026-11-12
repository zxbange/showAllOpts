import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { SystemConfigurationPage } from './SystemConfigurationPage'
const auth = vi.hoisted(() => ({ session: { user: { role: 'user' } } }))
vi.mock('../components/AccountAccess', () => ({ useAuth: () => auth }))
describe('system settings permissions', () => {
  it('hides editor from non-admin', () => {
    auth.session.user.role = 'user'
    const html = renderToStaticMarkup(createElement(SystemConfigurationPage))
    expect(html).toContain('仅管理员可访问')
    expect(html).not.toContain('<form')
  })
  it('mounts editor for admin', () => {
    auth.session.user.role = 'admin'
    expect(renderToStaticMarkup(createElement(SystemConfigurationPage))).toContain('重新读取配置')
  })
})
