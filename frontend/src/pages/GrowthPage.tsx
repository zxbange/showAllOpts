import { useCallback, useState } from 'react'
import {
  CalendarDays,
  RefreshCw,
  Search,
  TrendingUp,
  Layers3,
  CheckCheck,
  Info,
} from 'lucide-react'
import { getGrowthOverview } from '../api/client'
import { useResource } from '../hooks/useResource'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatCard } from '../components/StatCard'
import type { GrowthItem, GrowthReport, ReportGroup } from '../types/growth'
import '../styles/growth.css'

const groups: [ReportGroup, string][] = [
  ['annual', '年报'],
  ['q1', 'Q1 · 一季度'],
  ['q2', 'Q2 · 半年报'],
  ['q3', 'Q3 · 前三季'],
]
const number = (v: number | null | undefined, digits = 2) =>
  v == null
    ? '—'
    : v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const percent = (v: number | null) => (v == null ? '—' : number(v) + '%')

function ProfitBars({ reports, label }: { reports: GrowthReport[]; label: string }) {
  const values = reports.map((r) => r.net_profit / 1e8)
  const low = Math.min(0, ...values),
    high = Math.max(0, ...values)
  const span = high - low || 1
  const y = (v: number) => 68 - ((v - low) / span) * 58
  const zero = y(0)
  return (
    <div className="growth-mini">
      <div>
        <strong>{label}</strong>
        <small>归母净利润 / 亿元</small>
      </div>
      <svg viewBox="0 0 180 94" role="img" aria-label={label + '最近三期归母净利润'}>
        <line x1="9" x2="171" y1={zero} y2={zero} stroke="var(--border)" />
        {reports.map((r, i) => (
          <g key={r.report_period}>
            <title>
              {r.report_period}：归母净利润 {number(r.net_profit / 1e8, 4)} 亿元；EPS{' '}
              {number(r.eps, 4)} 元；净利润同比 {percent(r.profit_growth)}；EPS 同比{' '}
              {percent(r.eps_growth)}
            </title>
            <rect
              x={20 + i * 55}
              y={Math.min(y(values[i]), zero)}
              width="30"
              height={Math.max(1, Math.abs(y(values[i]) - zero))}
              rx="3"
              fill={values[i] < 0 ? '#277d70' : '#c74736'}
              opacity={0.4 + i * 0.3}
            />
            <text x={35 + i * 55} y="87" textAnchor="middle">
              {r.report_period.slice(0, 4)}
            </text>
          </g>
        ))}
      </svg>
      <div className="growth-mini-footer">
        <span>最新 {number(values.at(-1))} 亿</span>
        <b>+{percent(reports.at(-1)?.profit_growth ?? null)}</b>
      </div>
    </div>
  )
}

function StockCard({ stock }: { stock: GrowthItem }) {
  return (
    <article className="growth-stock panel">
      <div className="growth-stock-heading">
        <div className="growth-identity">
          <span className="growth-rank">{String(stock.rank).padStart(2, '0')}</span>
          <div>
            <h2>{stock.name}</h2>
            <span>
              {stock.code} · {stock.industry_name ?? '暂无行业'}
            </span>
          </div>
        </div>
        <div className="growth-annual">
          <small>年报最新归母净利润同比</small>
          <strong>+{percent(stock.annual_profit_growth)}</strong>
        </div>
      </div>
      <div className="growth-valuations">
        <span>
          PE / TTM <b>{number(stock.pe_ttm)}</b>
        </span>
        <span>
          PB <b>{number(stock.pb)}</b>
        </span>
        <span>
          总市值 <b>{number(stock.market_cap)} 亿</b>
        </span>
        <em>
          <CheckCheck size={14} />
          四类报告全部通过
        </em>
      </div>
      <div className="growth-charts">
        {groups.map(([key, name]) => (
          <ProfitBars key={key} reports={stock.reports[key]} label={name} />
        ))}
      </div>
      <details className="growth-evidence">
        <summary>查看 12 期财报与 16 项增长率</summary>
        <div
          className="growth-table-scroll"
          tabIndex={0}
          role="region"
          aria-label={stock.name + '财报明细，可左右滚动'}
        >
          <table>
            <thead>
              <tr>
                <th>报告类别</th>
                <th>报告期</th>
                <th>公告日期</th>
                <th>归母净利润 / 亿元</th>
                <th>净利润同比</th>
                <th>基本 EPS / 元</th>
                <th>EPS 同比</th>
              </tr>
            </thead>
            <tbody>
              {groups.flatMap(([key, label]) =>
                stock.reports[key].map((r) => (
                  <tr key={key + r.report_period}>
                    <td>{label}</td>
                    <td>{r.report_period}</td>
                    <td>{r.publication_date}</td>
                    <td>{number(r.net_profit / 1e8, 4)}</td>
                    <td>{percent(r.profit_growth)}</td>
                    <td>{number(r.eps, 4)}</td>
                    <td>{percent(r.eps_growth)}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
        <p>
          每类首期作为增长基期；半年报与前三季均为年初至报告期末累计值。各图独立刻度，可展开明细比较金额。
        </p>
      </details>
    </article>
  )
}

export function GrowthPage() {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const loader = useCallback((signal: AbortSignal) => getGrowthOverview(signal), [])
  const { data, error, loading, reload } = useResource(loader)
  const term = search.trim().toLowerCase()
  const items = (data?.items ?? []).filter((s) =>
    [s.code, s.name, s.industry_name ?? ''].some((v) => v.toLowerCase().includes(term)),
  )
  const pages = Math.max(1, Math.ceil(items.length / 20)),
    current = Math.min(page, pages)
  return (
    <div className="page-enter growth-page">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">SUSTAINED EARNINGS GROWTH</span>
          <h1>
            高增长 <span className="heading-tag">四类报告 · 双指标</span>
          </h1>
          <p>跨越年度与季度，寻找盈利持续增长的公司。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            截至 {data?.snapshot?.as_of_date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新高增长"
            disabled={loading}
            onClick={reload}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.snapshot ? (
        <EmptyState
          title="暂无高增长选股结果"
          description="请先在聚宽运行高增长采集脚本，入库后在「数据同步」中同步高增长模块。"
        />
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="全部入选"
              value={data.items.length}
              suffix="只"
              note="四类报告、16 项增长率全部达标"
              icon={<Layers3 size={18} />}
              accent
            />
            <StatCard
              label="年报最高净利润增速"
              value={number(data.items[0]?.annual_profit_growth)}
              suffix="%"
              note={data.items[0]?.name ?? '本次暂无入选'}
              icon={<TrendingUp size={18} />}
            />
            <StatCard
              label="增长门槛"
              value="20"
              suffix="%"
              note="净利润、EPS 各连续两次同比达标"
              icon={<CheckCheck size={18} />}
            />
            <StatCard
              label="完整财报股票"
              value={data.snapshot.evaluated_count}
              suffix="只"
              note={'股票池 ' + data.snapshot.universe_count + ' 只 · 每类取最新三期'}
              icon={<Layers3 size={18} />}
            />
          </div>
          <section className="growth-rule">
            <Info size={19} />
            <p>
              年报、Q1、半年报、前三季分别检查。每类最新三期的归母净利润与基本 EPS
              均连续两次同比增长 ≥ 20%，且最新一期均为正。按年报最近一次净利润增速降序排列。
            </p>
          </section>
          <div className="growth-toolbar">
            <div>
              <h2>
                持续增长清单 <span>{items.length}</span>
              </h2>
              <small>估值日期 {data.snapshot.valuation_date} · 仅保留最新名单</small>
            </div>
            <label>
              <Search size={17} />
              <input
                aria-label="搜索高增长股票"
                placeholder="股票代码、名称或行业"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value)
                  setPage(1)
                }}
              />
            </label>
          </div>
          {!data.items.length ? (
            <EmptyState
              title="本次暂无符合条件的股票"
              description="采集已完成，当前没有股票同时满足四类报告的全部条件。"
            />
          ) : !items.length ? (
            <EmptyState
              search
              title="没有匹配的股票"
              description="请尝试其他股票代码、名称或行业。"
            />
          ) : (
            <div className="growth-list">
              {items.slice((current - 1) * 20, current * 20).map((s) => (
                <StockCard key={s.code} stock={s} />
              ))}
            </div>
          )}
          {pages > 1 && (
            <nav className="growth-pagination" aria-label="高增长分页">
              <button
                className="button button-quiet"
                disabled={current <= 1}
                onClick={() => setPage(current - 1)}
              >
                上一页
              </button>
              <span>
                {current} / {pages}
              </span>
              <button
                className="button button-quiet"
                disabled={current >= pages}
                onClick={() => setPage(current + 1)}
              >
                下一页
              </button>
            </nav>
          )}
          <section className="method-card">
            <span className="method-icon">
              <Info size={19} />
            </span>
            <div>
              <h3>财报口径与计算规则</h3>
              <p>
                同比增长率 =（本期 − 上期）/
                |上期|。基期为零、指标缺失、同类报告年份不连续均不通过；历史亏损允许参与比较，但最新一期两项指标必须为正。各公司以截至采集日已披露的最新三期为准，报告年份可能不同。所有符合条件的股票都会展示。
              </p>
              <span>
                图表金额为归母净利润，EPS 为基本每股收益；未披露数据不补零。更新时间{' '}
                {data.snapshot.updated_at.replace('T', ' ').slice(0, 19)}（北京时间）。
              </span>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
