import { ShortTermNavigation, methods } from '../components/ShortTermNavigation'
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '../components/PageHeader'
import { useCallback, useEffect, useState } from 'react'
import { CandlestickChart, RefreshCw, ArrowUpRight } from 'lucide-react'
import { getShortTerm } from '../api/client'
import { useResource } from '../hooks/useResource'
import { LoadingState, ErrorState, EmptyState } from '../components/States'
import { ShortTermChart } from '../components/ShortTermChart'
import type { ShortMethod } from '../types/shortTerm'
import '../styles/short-term.css'

const fmt = (n: number | null | undefined, digits = 2) =>
  n == null ? '—' : n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
export function ShortTermPage() {
  const [params, setParams] = useSearchParams()
  const method: ShortMethod =
    methods.find((item) => item.id === params.get('method'))?.id ?? 'bukou'
  const day = params.get('day') || ''
  const setDay = (value: string) => setParams({ method, ...(value ? { day: value } : {}) })
  const [offset, setOffset] = useState(0)
  useEffect(() => {
    setOffset(0)
  }, [method, day])
  const loader = useCallback(
    (signal: AbortSignal) => getShortTerm(day, method, offset, signal),
    [day, method, offset],
  )
  const { data, error, loading, reload } = useResource(loader)
  const selected = methods.find((m) => m.id === method)!
  return (
    <div className="short-page page-enter">
      <PageHeader
        switcher={
          <ShortTermNavigation
            value={method}
            day={day}
            counts={data?.trade_date ? data.counts : undefined}
            loading={loading}
            error={!!error}
            hasSnapshot={!!data?.trade_date}
          />
        }
      >
        <div>
          <div className="short-title-row">
            <h1>
              短线选股 <span className="heading-tag">形态观察</span>
            </h1>
            <div className="short-controls">
              <label>
                选股日期{' '}
                <select
                  aria-label="短线选股日期"
                  value={day || data?.trade_date || ''}
                  onChange={(e) => {
                    setDay(e.target.value)
                    setOffset(0)
                  }}
                >
                  {!data?.dates.length && <option value={day}>{day || '暂无快照'}</option>}
                  {data?.dates.map((d) => (
                    <option key={d}>{d}</option>
                  ))}
                </select>
              </label>
              <button
                className="button button-quiet"
                onClick={reload}
                disabled={loading}
                aria-label="刷新短线选股"
              >
                <RefreshCw size={16} />
              </button>
            </div>
          </div>
          <p>按四种形态筛选股票，查看入选时的K线与均线。</p>
        </div>
        <CandlestickChart size={32} />
      </PageHeader>

      <section className="short-toolbar">
        <div>
          <h2>
            {selected.name}
            <ArrowUpRight size={18} />
          </h2>
          <p>{selected.note} · 统一剔除ST · 总市值≥30亿元</p>
        </div>
      </section>
      {method === 'oversold' && (
        <p className="short-summary">
          按所选日期收盘确认候选股；形态使用后复权价格和未复权成交量，K线沿用前复权展示。历史统计比较信号后第1、2、3日收盘涨幅，不代表未来胜率或可成交收益。
        </p>
      )}
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState
          message={error}
          retry={() => {
            setDay('')
            setOffset(0)
            reload()
          }}
        />
      ) : !data?.trade_date ? (
        <EmptyState
          title="尚未采集短线选股"
          description="请先在聚宽采集入库，再通过“数据同步”更新本模块。"
        />
      ) : method === 'oversold' && data.counts.oversold == null ? (
        <EmptyState
          title="此快照尚未计算超跌反弹"
          description="请运行新版短线选股采集脚本，再同步短线选股数据。"
        />
      ) : (
        <>
          <p className="short-summary">
            {data.trade_date} · 扫描 {fmt(data.meta.universe_count, 0)} 只 · {selected.name}入选{' '}
            {data.total} 只
          </p>
          {!data.items.length ? (
            <EmptyState title="本日暂无符合条件的股票" description="可切换战法或日期。" />
          ) : (
            <div className="short-stock-grid">
              {data.items.map((stock) => (
                <article key={stock.code} className="short-stock-card">
                  <header>
                    <div>
                      <h3>
                        {stock.name}
                        <small>{stock.code}</small>
                      </h3>
                      <span>{stock.industry || '暂无行业'}</span>
                    </div>
                    <div className={`short-price ${(stock.pct_change ?? 0) >= 0 ? 'up' : 'down'}`}>
                      <strong>{fmt(stock.close)}</strong>
                      <small>
                        {(stock.pct_change ?? 0) > 0 ? '+' : ''}
                        {fmt(stock.pct_change)}%
                      </small>
                    </div>
                  </header>
                  <div className="short-facts">
                    <span>
                      市值 <b>{fmt(stock.market_cap)} 亿</b>
                    </span>
                    <span>
                      PE <b>{fmt(stock.pe)}</b>
                    </span>
                    <span>
                      PB <b>{fmt(stock.pb)}</b>
                    </span>
                  </div>
                  <ShortTermChart name={stock.name} bars={stock.bars} />
                  <footer>
                    {stock.bars.length} 根 · {stock.bars[0]?.date} — {stock.bars.at(-1)?.date}
                    <span>
                      {stock.methods
                        .map((id) => methods.find((m) => m.id === id)?.name)
                        .join(' · ')}
                    </span>
                  </footer>
                </article>
              ))}
            </div>
          )}
          {data.total > 6 && (
            <nav className="short-pagination" aria-label="短线选股分页">
              <button
                className="button button-quiet"
                disabled={!offset}
                onClick={() => setOffset(Math.max(0, offset - 6))}
              >
                上一页
              </button>
              <span>
                {Math.floor(offset / 6) + 1} / {Math.ceil(data.total / 6)}
              </span>
              <button
                className="button button-quiet"
                disabled={offset + 6 >= data.total}
                onClick={() => setOffset(offset + 6)}
              >
                下一页
              </button>
            </nav>
          )}
        </>
      )}
    </div>
  )
}
