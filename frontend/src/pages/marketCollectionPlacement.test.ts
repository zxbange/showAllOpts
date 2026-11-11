import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketCollectionPanel } from '../components/MarketCollectionPanel'
import type { CrowdingHistory } from '../types/crowding'
import type { SentimentHistory } from '../types/sentiment'
import { CrowdingPage } from './CrowdingPage'
import { SentimentPage } from './SentimentPage'

const resource = vi.hoisted(() => ({
  data: null as CrowdingHistory | SentimentHistory | null,
  loading: false,
  error: null as string | null,
  reload: vi.fn(),
}))

vi.mock('../hooks/useResource', () => ({ useResource: () => resource }))
vi.mock('../components/CrowdingChart', () => ({ CrowdingChart: () => null }))
vi.mock('../components/SentimentChart', () => ({ SentimentChart: () => null }))
vi.mock('../components/MarketCollectionPanel', () => ({
  MarketCollectionPanel: vi.fn(() =>
    createElement('section', { 'aria-label': '市场日采集' }, 'Shared collection panel'),
  ),
}))

const summary = {
  observed_days: 1,
  average_ratio: 0.4,
  peak_ratio: 0.4,
  low_ratio: 0.4,
  change_pp: 0,
}
const crowding: CrowdingHistory = {
  top_pct: 0.05,
  start_date: '2026-11-10',
  end_date: '2026-11-10',
  latest: {
    date: '2026-11-10',
    ratio: 0.4,
    stock_count: 100,
    top_stock_count: 5,
    top_money: 400000000,
    total_money: 1000000000,
    updated_at: '2026-11-10T19:30:00+08:00',
  },
  summary,
  points: [{ date: '2026-11-10', ratio: 0.4, smoothed_ratio: 0.4 }],
}
const sentiment: SentimentHistory = {
  start_date: '2026-11-10',
  end_date: '2026-11-10',
  latest: {
    date: '2026-11-10',
    ratio: 0.4,
    above_count: 40,
    eligible_count: 100,
    universe_count: 110,
    state: 'normal',
    updated_at: '2026-11-10T19:30:00+08:00',
  },
  summary: { ...summary, cold_days: 0, hot_days: 0 },
  points: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  resource.data = null
  resource.loading = false
  resource.error = null
})

describe.each([
  {
    name: 'crowding',
    path: '/market-crowding',
    Page: CrowdingPage,
    history: crowding,
    methodology: '指标口径',
    emptyTitle: '暂无拥挤度数据',
  },
  {
    name: 'sentiment',
    path: '/market-sentiment',
    Page: SentimentPage,
    history: sentiment,
    methodology: '统计口径',
    emptyTitle: '暂无市场热度数据',
  },
])('$name collection placement', ({ path, Page, history, methodology, emptyTitle }) => {
  it.each(['populated', 'loading', 'error', 'empty', 'no response'] as const)(
    'keeps the shared panel last after methodology with the reload callback: %s',
    (state) => {
      if (state === 'populated') resource.data = history
      if (state === 'loading') resource.loading = true
      if (state === 'error') resource.error = 'History unavailable'
      if (state === 'empty') resource.data = { ...history, latest: null, points: [] }

      const html = renderToStaticMarkup(
        createElement(MemoryRouter, { initialEntries: [path] }, createElement(Page)),
      )
      const panelHtml = '<section aria-label="市场日采集">Shared collection panel</section>'
      expect(html).toContain(`<h3>${methodology}</h3>`)
      expect(html.indexOf(`<h3>${methodology}</h3>`)).toBeLessThan(html.indexOf(panelHtml))
      // Nothing may render after the shared panel inside the page root.
      expect(html.endsWith(`${panelHtml}</div>`)).toBe(true)
      expect(MarketCollectionPanel).toHaveBeenCalledTimes(1)
      const panelProps = vi.mocked(MarketCollectionPanel).mock.calls[0][0]
      expect(panelProps.onCollected).toBe(resource.reload)
      panelProps.onCollected()
      expect(resource.reload).toHaveBeenCalledTimes(1)

      if (state === 'populated') expect(html).toContain('40.00%')
      if (state === 'loading') expect(html).toContain('skeleton chart-skeleton')
      if (state === 'error') expect(html).toContain('History unavailable')
      if (state === 'empty') expect(html).toContain(emptyTitle)
    },
  )
})
