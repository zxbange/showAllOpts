import { useEffect, useRef, useState } from 'react'
import { init, use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { apiFetch } from '../api/http'
import { useAuth } from '../components/AccountAccess'
use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
])
const grades = ['CCI5500', 'CCI5000', 'CCI4500'] as const
type Quote = {
  quoteDate: string
  grade: string
  price: number
  change: number | null
  sourceUrl: string
  evidence: string
}
type Data = {
  rows: Quote[]
  missingChanges: number
  status: {
    running: boolean
    stage?: string
    error?: string
    checkedAt?: string
    targetDate?: string
    observed?: number
    inserted?: number
    changed?: number
    grades?: {
      grade: string
      returned: number | null
      latestQuoteDate: string | null
      targetHits: number
      error: string | null
    }[]
  }
}
const price = (v: number | null | undefined) => (v == null ? '—' : v.toLocaleString('zh-CN'))
function CoalChart({ rows }: { rows: Quote[] }) {
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!root.current) return
    const chart = init(root.current)
    const dates = [...new Set(rows.map((r) => r.quoteDate))].sort()
    const lookup = new Map(rows.map((r) => [`${r.quoteDate}:${r.grade}`, r]))
    chart.setOption({
      animation: false,
      aria: {
        enabled: true,
        description: 'CCI动力煤三档报价，人民币元每吨。来源为非官方网络汇集；缺失报价不填补。',
      },
      color: ['#26765e', '#b17c25', '#527baf'],
      legend: { data: grades, top: 0, bottom: 'auto' },
      grid: { left: 55, right: 20, top: 55, bottom: 65 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        confine: true,
        formatter: (items: { dataIndex: number }[]) => {
          const day = dates[items[0]?.dataIndex]
          if (!day) return ''
          return (
            `${day}\n` +
            grades
              .map((g) => {
                const q = lookup.get(`${day}:${g}`)
                return `${g}：${price(q?.price)}  涨跌：${price(q?.change)}`
              })
              .join('\n') +
            '\n人民币元/吨\n来源：coalindex.com（非官方）'
          )
        },
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { hideOverlap: true, formatter: (s: string) => s.slice(5) },
      },
      yAxis: { type: 'value', scale: true, name: '元/吨' },
      dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 8, height: 22 }],
      series: grades.map((grade) => ({
        name: grade,
        type: 'line',
        smooth: false,
        connectNulls: false,
        showSymbol: false,
        data: dates.map((day) => lookup.get(`${day}:${grade}`)?.price ?? null),
      })),
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(root.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [rows])
  return (
    <div className="commodity-chart" ref={root} role="img" aria-label="CCI动力煤三档价格走势" />
  )
}
export function CCICoalPage() {
  const admin = useAuth().session?.user?.role === 'admin'
  const [data, setData] = useState<Data | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const [grade, setGrade] = useState('all')
  const guard = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const r = await apiFetch('/api/v1/commodities/cci', {
          signal: controller.signal,
          cache: 'no-store',
        })
        if (!r.ok) throw Error('CCI报价暂时无法读取')
        const next = await r.json()
        if (!controller.signal.aborted) {
          setData(next)
          setError('')
          if (next.status.running) timer = setTimeout(poll, 2000)
        }
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message)
      }
    }
    void poll()
    return () => {
      mounted.current = false
      controller.abort()
      clearTimeout(timer)
    }
  }, [revision])
  async function refresh() {
    if (guard.current) return
    guard.current = true
    setBusy(true)
    try {
      const r = await apiFetch('/api/v1/commodities/cci/refresh', { method: 'POST' })
      if (!r.ok) throw Error('CCI更新未启动，请稍后重试')
      if (mounted.current) setRevision((v) => v + 1)
    } catch (e) {
      if (mounted.current) setError((e as Error).message)
    } finally {
      guard.current = false
      if (mounted.current) setBusy(false)
    }
  }
  return (
    <div className="commodity-page">
      <header>
        <span className="eyebrow">大宗商品 · 动力煤</span>
        <h1>CCI动力煤</h1>
        <p>三档人民币报价 · 元/吨 · 自2026-01-01</p>
      </header>
      <section className="commodity-summary">
        {grades.map((g) => {
          const latest = data?.rows.filter((r) => r.grade === g).at(-1)
          return (
            <div key={g}>
              <small>
                {g} · {latest?.quoteDate ?? '暂无报价'}
              </small>
              <strong>
                {price(latest?.price)} <small>元/吨</small>
              </strong>
              <small>来源涨跌：{price(latest?.change)}</small>
            </div>
          )
        })}
      </section>
      <p className="commodity-source-note">
        来源：
        <a href="https://coalindex.com/about" target="_blank" rel="noreferrer">
          coalindex.com 网络汇集（非官方）
        </a>
        。日期或涨跌缺失时留空，不补零、不沿用前值。
      </p>
      {(error || data?.status.error) && <p role="alert">{error || data?.status.error}</p>}
      <section className="commodity-panel">
        <div className="commodity-heading">
          <h2>价格走势</h2>
          {admin && (
            <button
              className="button"
              disabled={busy || data?.status.running}
              onClick={() => void refresh()}
            >
              {busy || data?.status.running ? '更新中…' : '更新今日'}
            </button>
          )}
        </div>
        <p>点击图例选择档位；仅显示来源实际报价日期，缺口不代表价格不变。</p>
        {data ? (
          data.rows.length ? (
            <CoalChart rows={data.rows} />
          ) : (
            <p>暂无可用CCI报价</p>
          )
        ) : (
          <p>正在读取报价…</p>
        )}
        <small>
          每天 21:00 自动采集 ·
          北京时间，仅查询当天；未完整取得时每30分钟重试，最多3次，跨日停止。保留既有历史。
          {data?.status.stage}
          {data?.status.checkedAt
            ? ` · 最近采集：${new Date(data.status.checkedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}`
            : ''}
        </small>
        {data?.status.targetDate && (
          <div role="status" aria-label="CCI当日更新状态">
            <p>
              目标日期：{data.status.targetDate} · 命中 {data.status.observed ?? 0}/3 档 · 新增{' '}
              {data.status.inserted ?? 0} · 变更 {data.status.changed ?? 0}
            </p>
            {data.status.grades?.map((result) => (
              <p key={result.grade}>
                {result.grade}：
                {result.error || (result.targetHits ? '已获取当日报价' : '来源无当日报价')}
                {' · 返回 '}
                {result.returned ?? '未知'} 条{' · 本次返回最新报价日期：'}
                {result.latestQuoteDate ?? '无'}
              </p>
            ))}
          </div>
        )}
      </section>
      <section className="commodity-panel">
        <div className="commodity-heading">
          <h2>历史报价</h2>
          <label>
            档位{' '}
            <select
              value={grade}
              onChange={(e) => setGrade(e.target.value)}
              aria-label="CCI报价档位"
            >
              <option value="all">全部</option>
              {grades.map((g) => (
                <option key={g}>{g}</option>
              ))}
            </select>
          </label>
        </div>
        <div
          className="commodity-table"
          tabIndex={0}
          role="region"
          aria-label="CCI历史报价，可滚动查看"
        >
          <table>
            <thead>
              <tr>
                {['报价日期', '档位', '价格', '来源涨跌', '来源 / 证据'].map((s) => (
                  <th key={s}>{s}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data?.rows
                .slice()
                .reverse()
                .filter((r) => grade === 'all' || r.grade === grade)
                .map((r) => (
                  <tr key={`${r.quoteDate}:${r.grade}`}>
                    <td>{r.quoteDate}</td>
                    <td>{r.grade}</td>
                    <td>{price(r.price)}</td>
                    <td>{price(r.change)}</td>
                    <td>
                      <a href="https://coalindex.com/market" target="_blank" rel="noreferrer">
                        公开查询 · 非官方 ↗
                      </a>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
