import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ConfigurationErrorDialog } from './ConfigurationErrorDialog'

describe('configuration failure dialog', () => {
  it('renders a dismissible accessible dialog with escaped error detail', () => {
    const html = renderToStaticMarkup(
      createElement(ConfigurationErrorDialog, { message: '<script>invalid</script>' }),
    )
    expect(html).toContain('aria-label="配置操作失败"')
    expect(html).toContain('method="dialog"')
    expect(html).toContain('知道了')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
  })
})
