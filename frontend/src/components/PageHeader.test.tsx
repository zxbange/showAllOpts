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
