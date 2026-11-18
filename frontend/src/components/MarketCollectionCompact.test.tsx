import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { MarketCollectionPanel } from './MarketCollectionPanel'
vi.mock('./AccountAccess', () => ({ useAuth: () => ({ session: { user: { role: 'admin' } } }) }))
vi.mock('../hooks/usePolling', () => ({ usePolling: () => ({ refresh: vi.fn() }) }))
vi.mock('./PageHeaderActions', () => ({
  PageHeaderActions: ({ children }: { children: React.ReactNode }) => (
    <div data-test-actions>{children}</div>
  ),
}))
it('renders real controller actions without restoring the obsolete card or heading', () => {
  const html = renderToStaticMarkup(<MarketCollectionPanel onCollected={() => {}} />)
  expect(html).toContain('更新数据')
  expect(html).toContain('disabled')
  expect(html).not.toContain('<section')
  expect(html).not.toContain('market-collection-panel')
  expect(html).not.toContain('<h2>')
})
