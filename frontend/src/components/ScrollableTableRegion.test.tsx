import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { ScrollableTableRegion } from './ScrollableTableRegion'

it('preserves the module scroll class, keyboard focus, region name and native table semantics', () => {
  const html = renderToStaticMarkup(
    <ScrollableTableRegion
      className="holdings-scroll holdings-history-scroll"
      label="仓位变动表，可上下左右滚动"
    >
      <table>
        <caption>仓位变动</caption>
        <thead>
          <tr>
            <th scope="col">日期</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>2026-11-25</td>
          </tr>
        </tbody>
      </table>
    </ScrollableTableRegion>,
  )
  expect(html).toBe(
    '<div class="holdings-scroll holdings-history-scroll" tabindex="0" role="region" aria-label="仓位变动表，可上下左右滚动"><table><caption>仓位变动</caption><thead><tr><th scope="col">日期</th></tr></thead><tbody><tr><td>2026-11-25</td></tr></tbody></table></div>',
  )
})
