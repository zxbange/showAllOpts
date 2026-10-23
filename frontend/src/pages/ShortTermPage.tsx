import { useCallback, useState } from 'react'
import { CandlestickChart, RefreshCw, ArrowUpRight, Waves, Flame, Zap } from 'lucide-react'
import { getShortTerm } from '../api/client'
import { useResource } from '../hooks/useResource'
import { LoadingState, ErrorState, EmptyState } from '../components/States'
import { ShortTermChart } from '../components/ShortTermChart'
import type { ShortMethod } from '../types/shortTerm'
import '../styles/short-term.css'

const methods = [
  { id: 'bukou' as ShortMethod, name: '补票龙', subtitle: '趋势中的回踩再起', icon: Waves, note: 'BBI向上 · 长RSV强势 · 短RSV回落再上行 · DIF为正' },
  { id: 'redhover' as ShortMethod, name: '红悬停龙', subtitle: '涨停后的高位承接', icon: Flame, note: '前两日涨停 · 随后两日低点守住阈值 · 最新日未涨停' },
  { id: 'xuli' as ShortMethod, name: '蓄力龙', subtitle: '缩量整理后的突破', icon: Zap, note: '阳—阴—阳 · 缩量回撤后放量 · MA10 > MA20 > MA30 > MA60' },
]
const fmt = (n: number | null | undefined, digits = 2) => n == null ? '—' : n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
export function ShortTermPage() {
  const [method, setMethod] = useState<ShortMethod>('bukou')
  const [day, setDay] = useState('')
  const [offset, setOffset] = useState(0)
  const loader = useCallback((signal: AbortSignal) => getShortTerm(day, method, offset, signal), [day, method, offset])
  const { data, error, loading, reload } = useResource(loader)
  const selected = methods.find(m => m.id === method)!
  return <div className="short-page page-enter">
    <div className="page-heading">
      <div><span className="eyebrow">SHORT TERM PATTERN LAB</span><h1>短线选股 <span className="heading-tag">形态观察</span></h1>
        <p>把信号放回走势里，观察价格、均线与量能的配合。</p></div><CandlestickChart size={32} />
    </div>
    <div className="short-date-toolbar"><div className="short-controls"><label>选股日期 <select aria-label="短线选股日期" value={day || data?.trade_date || ''}
        onChange={e => { setDay(e.target.value); setOffset(0) }}>
        {!data?.dates.length && <option value={day}>{day || '暂无快照'}</option>}
        {data?.dates.map(d => <option key={d}>{d}</option>)}
      </select></label><button className="button button-quiet" onClick={reload} disabled={loading} aria-label="刷新短线选股"><RefreshCw size={16} /></button></div></div>
    <div className="short-methods" role="tablist" aria-label="短线选股战法">
      {methods.map(m => <button key={m.id} role="tab" aria-selected={method === m.id}
        className={method === m.id ? 'active' : ''} onClick={() => { setMethod(m.id); setOffset(0) }}>
        <m.icon size={23} /><span><strong>{m.name}</strong><small>{m.subtitle}</small></span>
        <b>{data ? data.counts[m.id] ?? 0 : '—'}<small>只</small></b>
      </button>)}
    </div>
    <section className="short-toolbar">
      <div><h2>{selected.name}<ArrowUpRight size={18} /></h2><p>{selected.note} · 统一剔除ST · 总市值≥30亿元</p></div>

    </section>
    <p className="short-caption">保留最近 3 个交易日的选股快照 · 每只股票展示近 30 个交易日日K线 · 前复权 · MA5 / 10 / 20 / 30 / 60</p>
    {loading ? <LoadingState /> : error ? <ErrorState message={error} retry={() => { setDay(''); setOffset(0); reload() }} /> : !data?.trade_date ?
      <EmptyState title="尚未采集短线选股" description="在聚宽运行短线选股采集脚本，再通过数据同步加载本模块。" /> :
      <><p className="short-summary">{data.trade_date} · 扫描 {fmt(data.meta.universe_count, 0)} 只 · {selected.name}入选 {data.total} 只</p>
      {!data.items.length ? <EmptyState title="本日暂无符合条件的股票" description="该日筛选已完成，可以切换其他战法或日期查看。" /> :
        <div className="short-stock-grid">{data.items.map(stock => <article key={stock.code} className="short-stock-card">
          <header><div><h3>{stock.name}<small>{stock.code}</small></h3><span>{stock.industry || '暂无行业'}</span></div>
            <div className={`short-price ${(stock.pct_change ?? 0) >= 0 ? 'up' : 'down'}`}><strong>{fmt(stock.close)}</strong><small>{(stock.pct_change ?? 0) > 0 ? '+' : ''}{fmt(stock.pct_change)}%</small></div></header>
          <div className="short-facts"><span>市值 <b>{fmt(stock.market_cap)} 亿</b></span><span>PE <b>{fmt(stock.pe)}</b></span><span>PB <b>{fmt(stock.pb)}</b></span></div>
          <ShortTermChart name={stock.name} bars={stock.bars} />
          <footer>{stock.bars.length} 根 · {stock.bars[0]?.date} — {stock.bars.at(-1)?.date}<span>{stock.methods.map(id => methods.find(m => m.id === id)?.name).join(' · ')}</span></footer>
        </article>)}</div>}
      {data.total > 6 && <nav className="short-pagination" aria-label="短线选股分页">
        <button className="button button-quiet" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 6))}>上一页</button>
        <span>{Math.floor(offset / 6) + 1} / {Math.ceil(data.total / 6)}</span>
        <button className="button button-quiet" disabled={offset + 6 >= data.total} onClick={() => setOffset(offset + 6)}>下一页</button>
      </nav>}</>}
  </div>
}
