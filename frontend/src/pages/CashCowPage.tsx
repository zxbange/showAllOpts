import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { cashCowPeriod, cashCowUpdateTime, type CashCowPeriod } from '../lib/cashCow'
import { RefreshCw, Square } from 'lucide-react'
import { useAuth } from '../components/AccountAccess'
import { PageHeader } from '../components/PageHeader'
import { getCashCowLatest, getCashCowStatus, updateCashCow } from '../api/cashCow'
import type { CashCowOverview, CashCowStatus } from '../types/cashCow'
import '../styles/cash-cow.css'

const percent = (n: number) => `${n.toFixed(2)}%`
const amount = (n: number) => (n / 1e8).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const errorMessage = (e: unknown) => (e instanceof Error ? e.message : '暂时无法连接数据服务。')

export function CashCowPage() {
  const [params, setParams] = useSearchParams()
  const years = cashCowPeriod(params.get('years'))
  useEffect(() => {
    const raw = params.get('years')
    if (raw !== null && !['3', '5', '10'].includes(raw)) {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous)
          next.set('years', '10')
          return next
        },
        { replace: true },
      )
    }
  }, [params, setParams])
  return (
    <CashCowPeriodPage
      years={years}
      onPeriodChange={(value) =>
        setParams((previous) => {
          const next = new URLSearchParams(previous)
          next.set('years', String(value))
          return next
        })
      }
    />
  )
}

// Commands belong to the shared job and survive period changes. Only reads are
// aborted on switching views, so late responses cannot cross periods.
function CashCowPeriodPage({
  years,
  onPeriodChange,
}: {
  years: CashCowPeriod
  onPeriodChange: (years: CashCowPeriod) => void
}) {
  const admin = useAuth().session?.user?.role === 'admin'
  const [data, setData] = useState<CashCowOverview | null>(null)
  const [status, setStatus] = useState<CashCowStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const [query, setQuery] = useState('')
  const command = useRef<AbortController | null>(null)
  const loadedPeriod = useRef(years)
  const running = status?.running ?? false

  useEffect(() => () => command.current?.abort(), [])
  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    async function latest() {
      try {
        const next = await getCashCowLatest(controller.signal, years)
        if (controller.signal.aborted) return
        setData(next)
        setStatus(next.status)
        setError(null)
        if (next.status.running) timer = window.setTimeout(poll, 2000)
      } catch (e) {
        if (!controller.signal.aborted) setError(errorMessage(e))
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    async function poll() {
      try {
        const next = await getCashCowStatus(controller.signal, years)
        if (controller.signal.aborted) return
        setStatus(next)
        setError(null)
        if (!next.running) {
          await latest()
          return
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(`状态更新失败：${errorMessage(e)}`)
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 2000)
    }
    setLoading(true)
    if (loadedPeriod.current !== years) {
      setData(null)
      loadedPeriod.current = years
    }
    void latest()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [revision, years])

  async function act(action: 'refresh' | 'cancel') {
    if (command.current || loading || !status || status.cancelling || !admin) return
    const controller = new AbortController()
    command.current = controller
    setBusy(true)
    setError(null)
    try {
      await updateCashCow(action, controller.signal)
      if (!controller.signal.aborted) {
        setLoading(true)
        setRevision((v) => v + 1)
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(errorMessage(e))
    } finally {
      if (!controller.signal.aborted) {
        command.current = null
        setBusy(false)
      }
    }
  }

  const snapshot = data?.snapshot
  const items = (data?.items ?? []).filter((item) =>
    `${item.code} ${item.name}`.toLowerCase().includes(query.trim().toLowerCase()),
  )
  const shownError = error || (!running ? status?.error : null)
  return (
    <div className="page-enter cash-cow-page">
      <PageHeader>
        <div>
          <h1>奶牛股</h1>
          <p>近 {years} 年持续分红，兼顾利润与经营现金流。</p>
          <p>一次采集近 10 年数据，同时更新 3 / 5 / 10 年结果；切换年限仅查看结果。</p>
        </div>
        <div className="heading-actions">
          <label className="cash-cow-period">
            筛选年限{' '}
            <select
              aria-label="筛选年限"
              value={years}
              onChange={(event) => onPeriodChange(cashCowPeriod(event.target.value))}
            >
              {[3, 5, 10].map((value) => (
                <option key={value} value={value}>
                  近 {value} 年
                </option>
              ))}
            </select>
          </label>
          <span className="date-badge">数据截至 {snapshot?.as_of ?? '—'}</span>
          <button
            className="button button-primary"
            disabled={busy || loading || !status || !admin || status.cancelling}
            onClick={() => void act(running ? 'cancel' : 'refresh')}
          >
            {running ? <Square size={16} /> : <RefreshCw size={16} />}
            {busy
              ? '正在提交…'
              : status?.cancelling
                ? '正在停止…'
                : running
                  ? '停止采集'
                  : '更新全部周期'}
          </button>
        </div>
      </PageHeader>
      <section className="panel cash-cow-summary" aria-label="筛选概况">
        <div className="cash-cow-updates" aria-label="成功更新时间">
          <span>
            最近成功更新 <time>{cashCowUpdateTime(snapshot?.updated_at)}</time>
          </span>
          <span>
            上一次 <time>{cashCowUpdateTime(snapshot?.previous_updated_at)}</time>
          </span>
          <span>
            上上一次 <time>{cashCowUpdateTime(snapshot?.previous_previous_updated_at)}</time>
          </span>
          <span>北京时间</span>
        </div>
        <div className="cash-cow-status" role="status">
          {loading ? '正在读取结果…' : status?.stage || '等待更新'}
          {running && (
            <span>
              {' '}
              · {status?.processed.toLocaleString('zh-CN')} /{' '}
              {status?.total?.toLocaleString('zh-CN') ?? '—'}
            </span>
          )}
          {running && (
            <progress
              aria-label="奶牛股采集进度"
              max={status?.total || 1}
              value={status?.total ? status.processed : undefined}
            />
          )}
          {snapshot &&
            (running ||
              status?.cancelling ||
              shownError ||
              /取消|停止/.test(status?.stage ?? '')) && (
              <p>当前仍展示上次成功快照；失败或停止不会覆盖已保存结果。</p>
            )}
        </div>
        {snapshot && (
          <div className="cash-cow-counts">
            <strong>入选 {snapshot.matched_count} 只</strong>
            <span>
              {snapshot.start_year}–{snapshot.end_year}
            </span>
            <span>股票池 {snapshot.universe_count}</span>
            <span>已判定 {snapshot.evaluated_count}</span>
            <span>不符合 {snapshot.excluded_count}</span>
            <span>待核验 {snapshot.insufficient_count}</span>
          </div>
        )}
        {shownError && (
          <div className="cash-cow-error" role="alert">
            {shownError}
            {snapshot ? ' 已保留上次成功结果。' : ''}
          </div>
        )}
        {error && (
          <button
            className="button button-quiet"
            disabled={loading || busy}
            onClick={() => setRevision((v) => v + 1)}
          >
            重新读取
          </button>
        )}
        {!admin && <p>仅管理员可更新或停止采集。</p>}
        <details className="cash-cow-rules">
          <summary>筛选口径</summary>

          <p>
            近 {years}{' '}
            个完整年度逐年现金分红大于零、扣非归母净利润大于零，经营现金流净额覆盖现金分红，股利支付率为
            30%–70%。{years} 年年度股息率的算术平均值大于 4%。
          </p>
          <p>
            股利支付率＝现金分红总额 ÷ 归母净利润；现金流覆盖＝经营现金流净额 ÷
            现金分红总额；年度股息率＝年度每股现金分红 ÷
            该年平均每日收盘价。送转股、拆并股需统一每股口径；资料不足或口径不确定的股票待核验，不直接判定合格。当前股息率＝过去12个月已实施每股现金分红
            ÷ 最新收盘价，仅供比较。
          </p>
          {snapshot && (
            <p>
              来源：{snapshot.source} · 更新于 {cashCowUpdateTime(snapshot.updated_at)}（北京时间）
            </p>
          )}
        </details>
      </section>
      <section className="panel cash-cow-results" aria-label="奶牛股筛选结果" aria-busy={loading}>
        <div className="cash-cow-toolbar">
          <h2>入选股票</h2>
          <input
            type="search"
            aria-label="搜索入选股票"
            placeholder="股票名称 / 代码"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        {!snapshot ? (
          <div className="state-panel">
            <h3>{loading ? '正在加载' : '暂无筛选快照'}</h3>
            <p>点击“更新全部周期”从公开接口采集并筛选，首次运行可能需要较长时间。</p>
          </div>
        ) : !data?.items.length ? (
          <div className="state-panel">
            <h3>
              {snapshot.insufficient_count ? '已判定股票中暂无入选' : '本次筛选无符合条件的股票'}
            </h3>
            {snapshot.insufficient_count > 0 && (
              <p>另有 {snapshot.insufficient_count} 只资料不足，尚不能判断是否符合。</p>
            )}
          </div>
        ) : !items.length ? (
          <div className="state-panel">没有匹配的入选股票</div>
        ) : (
          items.map((item) => (
            <details className="cash-cow-stock" key={item.code}>
              <summary>
                <span>
                  <strong>{item.name}</strong>
                  <small>
                    {item.code} · {item.exchange}
                  </small>
                </span>
                <span>
                  <small>近 {years} 年平均股息率</small>
                  <strong>{percent(item.average_yield)}</strong>
                </span>
                <span
                  title={
                    item.latest_price_date
                      ? `最新收盘价 ${item.latest_price?.toFixed(2) ?? '—'} 元（${item.latest_price_date}）；过去12个月已实施每股分红 ${item.dividend_ttm_per_share?.toFixed(4) ?? '—'} 元`
                      : '当前数据待核验'
                  }
                >
                  <small>当前股息率（TTM）</small>
                  {item.current_yield == null ? '—' : percent(item.current_yield)}
                </span>
                <span>
                  <small>股利支付率区间</small>
                  {percent(item.payout_min)}–{percent(item.payout_max)}
                </span>
                <span>
                  <small>最低现金流覆盖</small>
                  {item.coverage_min.toFixed(2)} 倍
                </span>
                <span className="cash-cow-expand">年度明细</span>
              </summary>
              <div
                className="cash-cow-table"
                tabIndex={0}
                role="region"
                aria-label={`${item.name}年度明细`}
              >
                <table>
                  <caption>金额单位：亿元；每股分红与收盘价：元</caption>
                  <thead>
                    <tr>
                      {[
                        '年度',
                        '每股分红',
                        '现金分红',
                        '扣非归母净利润',
                        '归母净利润',
                        '经营现金流',
                        '年均收盘价',
                        '年末交易日',
                        '股利支付率',
                        '现金流覆盖',
                        '年度股息率',
                      ].map((label) => (
                        <th key={label} scope="col">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {item.years.map((year) => (
                      <tr key={year.year}>
                        <th scope="row">{year.year}</th>
                        <td>{year.dividend_per_share.toFixed(4)}</td>
                        <td>{amount(year.cash_dividend)}</td>
                        <td>{amount(year.adjusted_profit)}</td>
                        <td>{amount(year.net_profit)}</td>
                        <td>{amount(year.operating_cashflow)}</td>
                        <td>{year.year_average_close.toFixed(2)}</td>
                        <td>{year.price_date}</td>
                        <td>{percent(year.payout_ratio)}</td>
                        <td>{year.cashflow_coverage.toFixed(2)} 倍</td>
                        <td>{percent(year.dividend_yield)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ))
        )}
      </section>
    </div>
  )
}
