import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { CollectionHistoryFrame, CollectionHistoryError } from './CollectionHistory'
it('matches the existing ETF disclosure/list structure without inventing empty success', () => {
  const html = renderToStaticMarkup(<CollectionHistoryFrame hasRecords={false} />)
  expect(html).toContain('<summary>最近采集记录</summary>')
  expect(html).toContain('暂无已完成的采集或同步记录')
  expect(html).not.toContain('<li>')
  expect(html).not.toContain('成功')
})
it('displays load errors distinctly from an empty journal and preserves failed rows', () => {
  const error = renderToStaticMarkup(<CollectionHistoryFrame hasRecords={false} error="读取失败" />)
  expect(error).toContain('读取失败')
  expect(error).not.toContain('暂无已完成')
  const html = renderToStaticMarkup(
    <CollectionHistoryFrame hasRecords>
      <li>自动采集 · 失败</li>
      <li>手动采集 · 成功</li>
    </CollectionHistoryFrame>,
  )
  expect(html).toContain('<ul><li>自动采集 · 失败</li><li>手动采集 · 成功</li></ul>')
})
it('shows safe error details as escaped text and labels the real failure stage', () => {
  const html = renderToStaticMarkup(
    <CollectionHistoryError
      details={{
        code: 'database_timeout',
        message: '<script>alert(1)</script> 数据库读取超时',
        stage: 'database_write',
        mysql_errno: 2013,
      }}
      failed
    />,
  )
  expect(html).toContain('<summary>错误详情</summary>')
  expect(html).toContain('&lt;script&gt;')
  expect(html).not.toContain('<script>')
  expect(html).toContain('失败阶段：写入数据库')
  expect(html).toContain('MySQL 错误码：2013')
})
it('does not invent a specific cause when an old failed record has no details', () => {
  const html = renderToStaticMarkup(<CollectionHistoryError failed />)
  expect(html).toContain('未记录详细错误原因')
  expect(html).not.toContain('网络')
  expect(renderToStaticMarkup(<CollectionHistoryError />)).toBe('')
})
it('ignores unknown stage and arbitrary extra error payload fields', () => {
  const details = {
    code: 'unknown',
    message: '采集失败',
    stage: 'raw-secret-stage',
    sql: 'SELECT secret',
    token: 'sensitive',
  }
  const html = renderToStaticMarkup(<CollectionHistoryError details={details} />)
  expect(html).not.toContain('raw-secret-stage')
  expect(html).not.toContain('SELECT secret')
  expect(html).not.toContain('sensitive')
})
it('displays only valid HTTP status and strict dated failure context', () => {
  const html = renderToStaticMarkup(
    <CollectionHistoryError
      details={{
        code: 'http_error',
        message: '数据源响应异常',
        http_status: 503,
        trade_date: '2026-11-18',
        start_date: '2026-11-16',
        end_date: '2026-11-18',
      }}
    />,
  )
  expect(html).toContain('HTTP 状态码：503')
  expect(html).toContain('交易日期：2026-11-18')
  expect(html).toContain('窗口开始日期：2026-11-16')
  expect(html).toContain('窗口结束日期：2026-11-18')
})
it('does not invent missing status or dates and rejects malformed context', () => {
  for (const http_status of [99, 600, 503.5, NaN]) {
    const html = renderToStaticMarkup(
      <CollectionHistoryError
        details={{
          code: 'unknown',
          message: '失败',
          http_status,
          trade_date: '2026-02-30',
          start_date: '2026-11-18T00:00:00Z',
          end_date: '<script>x</script>',
        }}
      />,
    )
    expect(html).not.toContain('HTTP 状态码')
    expect(html).not.toContain('交易日期')
    expect(html).not.toContain('窗口开始日期')
    expect(html).not.toContain('窗口结束日期')
  }
  const html = renderToStaticMarkup(
    <CollectionHistoryError details={{ code: 'unknown', message: '失败' }} />,
  )
  expect(html).not.toContain('HTTP 状态码')
  expect(html).not.toContain('日期')
})

it('uses the shared compact journal styling without redundant timezone text', () => {
  const html = renderToStaticMarkup(
    <CollectionHistoryFrame hasRecords>
      <li data-outcome="failed">
        <div className="collection-history-row-text">自动采集 · 失败</div>
      </li>
    </CollectionHistoryFrame>,
  )
  expect(html).toContain('class="collection-history"')
  expect(html).toContain('data-outcome="failed"')
  expect(html).toContain('显示最近 5 次记录')
  expect(html).not.toContain('北京时间')
})
