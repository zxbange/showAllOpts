import { useCallback } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, FileText } from 'lucide-react'
import { getEarningsDetail } from '../api/client'
import { useResource } from '../hooks/useResource'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { EarningsChart } from '../components/EarningsChart'
import { decimal, industryLevel, percent, quarter, sourceNames } from '../lib/earnings'
import '../styles/earnings.css'

export function EarningsDetailPage() {
  const { code = '' } = useParams()
  const [params] = useSearchParams()
  const period = params.get('period') ?? ''
  const loader = useCallback(
    (signal: AbortSignal) => getEarningsDetail(code, period, signal),
    [code, period],
  )
  const { data, loading, error, reload } = useResource(loader)
  return (
    <div className="page-enter earnings-page">
      <Link className="earnings-back" to={`/earnings-gap?period=${encodeURIComponent(period)}`}>
        <ArrowLeft size={16} />
        返回入选列表
      </Link>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        data && (
          <>
            <div className="page-heading">
              <div>
                <span className="section-eyebrow">{data.code} / EARNINGS EVIDENCE</span>
                <h1>
                  {data.name}
                  <span className="heading-tag">{quarter(data.report_period)}</span>
                </h1>
                <p>
                  {data.industry_name ?? '行业未提供'} · {industryLevel(data.industry_level)} · 信号{' '}
                  {data.signal_date} · 公告 {data.announcement_date}
                </p>
              </div>
            </div>
            <div className="earnings-detail-metrics">
              {[
                ['归母净利同比', percent(data.profit_yoy)],
                ['总市值（亿元）', decimal(data.market_cap)],
                ['流通市值（亿元）', decimal(data.circulating_market_cap)],
                ['PE（TTM）', decimal(data.pe_ttm)],
                ['PB', decimal(data.pb)],
                ['高开幅度', percent(data.open_gap_pct)],
              ].map(([label, value]) => (
                <div key={label}>
                  <small>{label}</small>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <p className="earnings-note">
              利润增幅依据：{sourceNames[data.signal_source]} · {data.profit_basis}
              {data.signal_source === 'forecast' ? ' · 预告下限' : ''}。估值日期：
              {data.valuation_date}。
            </p>
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>信号附近的价格表现</h2>
                  <p>前 10、后最多 20 个交易日 · 前复权 · 仅已完成日线</p>
                </div>
              </div>
              {data.prices.length ? (
                <EarningsChart data={data} />
              ) : (
                <EmptyState title="暂无日线记录" description="重新采集后可查看价格走势。" />
              )}
            </section>
            <div className="earnings-evidence-heading">
              <span className="section-eyebrow">FOLLOW THE EVIDENCE</span>
              <h2>同报告期公告</h2>
            </div>
            <div className="earnings-disclosures">
              {Object.entries(sourceNames).map(([kind, name]) => {
                const events = data.events
                  .filter((e) => e.kind === kind)
                  .sort((a, b) => b.publication_date.localeCompare(a.publication_date))
                return (
                  <section className="panel earnings-disclosure" key={kind}>
                    <h2>
                      <FileText size={18} />
                      {name}
                      <span>{events.length} 条</span>
                    </h2>
                    {!events.length ? (
                      <p className="earnings-note">未取得该期披露数据</p>
                    ) : (
                      events.map((e, i) => (
                        <article key={i}>
                          <div className="earnings-event-heading">
                            <strong>{e.publication_date}</strong>
                            <span className={e.signal ? 'earnings-hit' : 'earnings-chip'}>
                              {e.signal ? '触发价格信号' : e.eligible ? '业绩达标' : '补充披露'}
                            </span>
                          </div>
                          <dl>
                            <div>
                              <dt>报告期 / 口径</dt>
                              <dd>
                                {e.report_period} / {e.profit_basis}
                              </dd>
                            </div>
                            <div>
                              <dt>归母净利润（元）</dt>
                              <dd>
                                {e.net_profit == null
                                  ? '未提供统一金额；见原始字段'
                                  : decimal(e.net_profit)}
                              </dd>
                            </div>
                            <div>
                              <dt>{kind === 'forecast' ? '同比预告区间' : '归母净利同比'}</dt>
                              <dd>
                                {percent(e.profit_yoy)}
                                {kind === 'forecast' && <> ～ {percent(e.profit_yoy_max)}</>}
                              </dd>
                            </div>
                            {e.signal && (
                              <div>
                                <dt>信号日 / 高开幅度</dt>
                                <dd>
                                  {e.signal.date} / {percent(e.signal.open_gap_pct)}
                                </dd>
                              </div>
                            )}
                          </dl>
                          {e.reason && (
                            <div className="earnings-source-reason">
                              <strong>公告原因字段（{e.reason_field}）</strong>
                              <p>{e.reason}</p>
                            </div>
                          )}
                          <details className="earnings-raw">
                            <summary>查看数据源原始字段</summary>
                            <p>原始字段单位见聚宽表说明。</p>
                            <pre>{JSON.stringify(e.raw, null, 2)}</pre>
                          </details>
                        </article>
                      ))
                    )}
                  </section>
                )
              })}
            </div>
            <p className="earnings-note">
              本页按公告日期关联行情，不能确认盘前或盘后发布时间。同比增长不等于超出市场预期。
            </p>
          </>
        )
      )}
    </div>
  )
}
