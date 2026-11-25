import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { Square } from 'lucide-react'
import { DisplayRefreshButton, PageActionButton } from './PageActionButton'
import {
  HistoryRangeSwitch,
  compactHistoryRanges,
  standardHistoryRanges,
} from './HistoryRangeSwitch'
import { TrendPanelHeading } from './TrendPanelHeading'

it('keeps the native collection action label, tone, disabled state and decorative icon', () => {
  const html = renderToStaticMarkup(
    <PageActionButton tone="quiet" icon={Square} iconSize={15} disabled onClick={vi.fn()}>
      正在停止…
    </PageActionButton>,
  )
  expect(html).toContain('type="button"')
  expect(html).toContain('class="button button-quiet"')
  expect(html).toContain('disabled=""')
  expect(html).toContain('aria-hidden="true"')
  expect(html).toContain('width="15"')
  expect(html).toContain('正在停止…')
})

it('keeps display refresh icon-only, labelled and independent of collection actions', () => {
  const refresh = vi.fn()
  const loading = renderToStaticMarkup(
    <DisplayRefreshButton
      onClick={refresh}
      ariaLabel="刷新估值展示数据"
      loading
      iconButton={false}
    />,
  )
  expect(loading).toContain('class="button button-quiet"')
  expect(loading).toContain('aria-label="刷新估值展示数据"')
  expect(loading).toContain('disabled=""')
  expect(loading).toContain('spin')
  expect(loading).not.toContain('更新数据')
  const readOnly = renderToStaticMarkup(
    <DisplayRefreshButton onClick={refresh} ariaLabel="刷新ETF热度" loading animate={false} />,
  )
  expect(readOnly).toContain('icon-button')
  expect(readOnly).not.toContain('spin')
  expect(refresh).not.toHaveBeenCalled()
})

it('retains standard and compact history labels and highlights only the selected option', () => {
  const select = vi.fn()
  const html = renderToStaticMarkup(
    <HistoryRangeSwitch
      label="拥挤度历史范围"
      options={standardHistoryRanges}
      value={90}
      onChange={select}
    />,
  )
  expect(html).toContain('role="group"')
  expect(html).toContain('aria-label="拥挤度历史范围"')
  expect(html).toContain('aria-pressed="true" class="active">近 3 月')
  expect(html.match(/aria-pressed="true"/g)).toHaveLength(1)
  expect(compactHistoryRanges.map((range) => range.label)).toEqual([
    '近1月',
    '近3月',
    '近半年',
    '近1年',
    '全部',
  ])
  expect(select).not.toHaveBeenCalled()
})

it('supports an explicitly custom chart span without pressing an unrelated preset', () => {
  const html = renderToStaticMarkup(
    <HistoryRangeSwitch
      label="创红比历史范围"
      options={[
        { value: 1, label: '近1年' },
        { value: 0, label: '全部' },
      ]}
      value={null}
      onChange={vi.fn()}
    />,
  )
  expect(html.match(/aria-pressed="false"/g)).toHaveLength(2)
  expect(html).not.toContain('aria-pressed="true"')
})

it('keeps chart heading slots in the existing panel DOM with business badges and links', () => {
  const html = renderToStaticMarkup(
    <TrendPanelHeading
      title="两融担保比例走势"
      description="按官方披露日期记录"
      badge={<span className="sentiment-state">正常</span>}
      actions={<a href="/source">查看数据来源</a>}
    />,
  )
  expect(html).toContain('class="panel-heading"')
  expect(html).toContain('class="panel-title"')
  expect(html).toContain('<h2>两融担保比例走势</h2>')
  expect(html).toContain('class="status-dot" aria-hidden="true"')
  expect(html).toContain('class="sentiment-state"')
  expect(html).toContain('<p>按官方披露日期记录</p>')
  expect(html).toContain('<a href="/source">查看数据来源</a>')
  const simple = renderToStaticMarkup(<TrendPanelHeading title="市场强度" statusDot={false} />)
  expect(simple).not.toContain('status-dot')
  expect(simple).toContain('<h2>市场强度</h2>')
})
