import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { expect, it } from 'vitest'
import { PageHeader } from './PageHeader'

function render(path: string, value?: string) {
  return renderToStaticMarkup(
    <MemoryRouter initialEntries={[path]}>
      <PageHeader dataUpdatedAt={value}>
        <h1>标题</h1>
      </PageHeader>
    </MemoryRouter>,
  )
}
it('shows explicit unknown data time on personal holdings, never a made-up time', () => {
  expect(render('/personal-holdings')).toContain('未提供可确认的秒级数据时间')
})
it('shows verified timestamps in Beijing with seconds, not business dates', () => {
  expect(render('/etf-shares', '2026-11-18T01:02:03Z')).toContain('2026-11-18 09:02:03')
  expect(render('/etf-shares', '2026-11-18')).toContain('未提供可确认的秒级数据时间')
})
it('does not label the landing page as a data collection page', () => {
  expect(render('/')).not.toContain('数据更新时间')
})
it('places the single timestamp in the upper-right heading badge without timezone copy', () => {
  const html = render('/etf-shares', '2026-11-18T01:02:03Z')
  expect(html).toMatch(/heading-actions page-heading-right[\s\S]*date-badge page-data-updated/)
  expect(html.match(/aria-label="数据更新时间"/g)).toHaveLength(1)
  expect(html).not.toContain('北京时间')
  expect(html).not.toContain('<p class="page-data-updated"')
  expect(html.indexOf('page-data-updated')).toBeLessThan(html.indexOf('page-header-toolbar'))
})
it('preserves existing heading controls alongside the timestamp', () => {
  const html = renderToStaticMarkup(
    <MemoryRouter initialEntries={['/etf-shares']}>
      <PageHeader dataUpdatedAt="2026-11-18T01:02:03Z">
        <div>
          <h1>标题</h1>
        </div>
        <div className="heading-actions">
          <button>刷新</button>
        </div>
      </PageHeader>
    </MemoryRouter>,
  )
  expect(html).toContain('<button>刷新</button>')
  expect(html).toContain('2026-11-18 09:02:03')
})
it('uses a two-column heading and a compact stacked time label', () => {
  const html = render('/etf-shares', '2026-11-18T01:02:03Z')
  expect(html).toContain('page-heading page-heading-with-time')
  expect(html).toContain('<span class="page-data-updated-label">数据更新时间</span>')
  expect(html).toContain('<span class="page-data-updated-value">2026-11-18 09:02:03</span>')
  expect(render('/')).not.toContain('page-heading-with-time')
})
