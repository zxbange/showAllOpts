import { PageHeader } from '../components/PageHeader'
import { ModuleSwitch } from '../components/ModuleSwitch'
import { Coins } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { CCICoalPage } from './CCICoalPage'
import { useEffect, useRef, useState } from 'react'
import { init, use } from 'echarts/core'
import { LineChart, CustomChart } from 'echarts/charts'
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
import '../styles/commodities.css'
use([
  LineChart,
  CustomChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
])
type Quote = {
  quoteDate: string
  low: number | null
  high: number | null
  mean: number
  change: number
  sourceUrl: string
  rangeEvidence: string
}
type Data = {
  rows: Quote[]
  status: {
    running: boolean
    error?: string
    stage?: string
    automatic: boolean
    checkedAt?: string
    pagesFetched?: number
    pagesParsed?: number
    inserted?: number
    changed?: number
    latestQuoteDate?: string
    noNew?: boolean
    currentVerified?: boolean
    stale?: boolean
    targetDate?: string
    targetMode?: string
    recovery?: { day: string; result: string }
    pendingCount?: number
    pendingTargets?: { day: string; reason: string }[]
    targetResult?: string
  }
}
const price = (n: number | null) => (n == null ? '未核验' : n.toLocaleString('zh-CN'))
function QuoteChart({ rows }: { rows: Quote[] }) {
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!root.current) return
    const chart = init(root.current)
    chart.setOption({
      animation: false,
      aria: {
        enabled: true,
        description: '长江现货A00铝每日低高区间与均价，单位元每吨；非开高低收K线。',
      },
      grid: { left: 65, right: 20, top: 50, bottom: 65 },
      legend: { data: ['报价区间', '均价'], top: 0, bottom: 'auto' },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        confine: true,
        formatter: (items: any[]) => {
          const q = rows[items[0]?.dataIndex]
          return q
            ? `${q.quoteDate}\n最低：${price(q.low)}\n最高：${price(q.high)}\n均价：${price(q.mean)}\n涨跌：${q.change > 0 ? '+' : ''}${q.change}\n单位：元/吨\n来源：长江有色金属网（长江现货）`
            : ''
        },
      },
      xAxis: {
        type: 'category',
        data: rows.map((r) => r.quoteDate),
        axisLabel: { hideOverlap: true, formatter: (s: string) => s.slice(5) },
      },
      yAxis: { type: 'value', scale: true, name: '元/吨' },
      dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 8, height: 22 }],
      series: [
        {
          name: '报价区间',
          type: 'custom',
          itemStyle: { color: '#a87922' },
          encode: { x: 0, y: [1, 2] },
          data: rows.map((q, i) => [i, q.low, q.high]),
          renderItem: (_p: any, api: any) => {
            const lo = api.value(1),
              hi = api.value(2)
            if (
              lo == null ||
              hi == null ||
              !Number.isFinite(Number(lo)) ||
              !Number.isFinite(Number(hi))
            )
              return
            const a = api.coord([api.value(0), lo]),
              b = api.coord([api.value(0), hi])
            return {
              type: 'group',
              children: [
                {
                  type: 'line',
                  shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] },
                  style: { stroke: '#a87922', lineWidth: 2 },
                },
                {
                  type: 'line',
                  shape: { x1: a[0] - 4, y1: a[1], x2: a[0] + 4, y2: a[1] },
                  style: { stroke: '#a87922' },
                },
                {
                  type: 'line',
                  shape: { x1: b[0] - 4, y1: b[1], x2: b[0] + 4, y2: b[1] },
                  style: { stroke: '#a87922' },
                },
              ],
            }
          },
        },
        {
          name: '均价',
          type: 'line',
          data: rows.map((q) => q.mean),
          symbol: 'circle',
          symbolSize: 7,
          lineStyle: { color: '#26765e', width: 1.5 },
          itemStyle: { color: '#26765e' },
          connectNulls: false,
        },
      ],
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(root.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [rows])
  return (
    <div ref={root} className="commodity-chart" role="img" aria-label="A00铝每日报价区间与均价图" />
  )
}
function A00Page() {
  const admin = useAuth().session?.user?.role === 'admin'
  const [data, setData] = useState<Data | null>(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0)
  const guard = useRef(false),
    mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const r = await apiFetch('/api/v1/commodities/a00', {
          signal: controller.signal,
          cache: 'no-store',
        })
        if (!r.ok) throw Error('报价暂时无法读取')
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
      const r = await apiFetch('/api/v1/commodities/a00/refresh', {
        method: 'POST',
      })
      if (!r.ok) throw Error('采集未启动，请稍后重试')
      if (mounted.current) setRevision((v) => v + 1)
    } catch (e) {
      if (mounted.current) setError((e as Error).message)
    } finally {
      guard.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const latest = data?.rows.at(-1)
  return (
    <div className="commodity-page">
      <section className="commodity-summary">
        <div>
          <small>最近报价 · {latest?.quoteDate ?? '—'}</small>
          <strong>
            {latest ? price(latest.mean) : '—'} <small>元/吨</small>
          </strong>
        </div>
        <div>
          <small>当日涨跌</small>
          <strong>{latest ? `${latest.change > 0 ? '+' : ''}${latest.change}` : '—'}</strong>
        </div>
        <div>
          <small>数据口径</small>
          <p>现货报价，不是开高低收K线</p>
        </div>
      </section>
      {(error || data?.status.error) && <p role="alert">{error || data?.status.error}</p>}
      <section className="commodity-panel">
        <div className="commodity-heading">
          <h2>价格走势</h2>
          {admin && (
            <div className="commodity-actions">
              <button
                className="button"
                disabled={busy || data?.status.running}
                onClick={() => void refresh()}
              >
                {busy || data?.status.running ? '采集中…' : '更新当日报价'}
              </button>
            </div>
          )}
        </div>
        <p>
          竖线表示最低至最高报价，绿点与连线表示均价。区间未核验的日期仅显示均价，不推算低高价。
        </p>
        {data ? (
          data.rows.length ? (
            <QuoteChart rows={data.rows} />
          ) : (
            <p>暂无已核验报价</p>
          )
        ) : (
          <p>正在读取报价…</p>
        )}
        <div className="commodity-collection-status" role="status" aria-live="polite">
          <strong>{data?.status.running ? '采集中' : data?.status.stage || '暂无采集记录'}</strong>
          <span>
            最近采集：
            {data?.status.checkedAt
              ? new Date(data.status.checkedAt).toLocaleString('zh-CN', {
                  timeZone: 'Asia/Shanghai',
                  hour12: false,
                })
              : '—'}
          </span>
          {data?.status.targetDate && (
            <span>
              本次目标：{data.status.targetDate} ·{' '}
              {data.status.targetMode === 'complete_spot' ? '低 / 高 / 均价' : '最新现货价'} ·{' '}
              {
                (
                  {
                    current: '已核验',
                    sourceNotPublished: '首页尚未发布',
                    targetDateUnavailable: '未取得该日报价',
                    partial: '字段不完整',
                    unverified: '无法核验',
                    sourceReadFailed: '读取失败',
                    interrupted: '已中断',
                    pending: '等待读取',
                  } as Record<string, string>
                )[data.status.targetResult || 'pending']
              }
            </span>
          )}
          <span>最新报价：{data?.status.latestQuoteDate || latest?.quoteDate || '—'}</span>
          {data?.status.pagesFetched != null && (
            <span>
              读取 {data.status.pagesFetched} 页 · 解析 {data.status.pagesParsed ?? 0} 页 · 新增{' '}
              {data.status.inserted ?? 0} 条 · 更新 {data.status.changed ?? 0} 条
            </span>
          )}
          {!data?.status.running && data?.status.noNew && data.status.currentVerified && (
            <span>暂无新报价</span>
          )}
          {!data?.status.running && data?.status.stale && <span>已保存报价尚未更新至今日</span>}
        </div>
      </section>
      <section className="commodity-panel">
        <h2>每日报价</h2>
        <div
          className="commodity-table"
          tabIndex={0}
          role="region"
          aria-label="每日报价，可滚动查看"
        >
          <table>
            <thead>
              <tr>
                {['报价日期', '最低', '最高', '均价', '涨跌', '核验 / 来源'].map((h) => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data?.rows
                .slice()
                .reverse()
                .map((q) => (
                  <tr key={q.quoteDate}>
                    <td>{q.quoteDate}</td>
                    <td>{price(q.low)}</td>
                    <td>{price(q.high)}</td>
                    <td>{price(q.mean)}</td>
                    <td>
                      {q.change > 0 ? '+' : ''}
                      {q.change}
                    </td>
                    <td>
                      <a href={q.sourceUrl} target="_blank" rel="noreferrer">
                        {q.rangeEvidence === 'page' ? '正文已核验' : '均价已核验 · 区间待核验'} ↗
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

export function CommoditiesPage() {
  const [params] = useSearchParams()
  const coal = params.get('instrument') === 'cci'
  return (
    <div className="commodity-page">
      <PageHeader
        switcher={
          <ModuleSwitch
            label="大宗商品品种"
            value={coal ? 'cci' : 'a00'}
            items={[
              {
                id: 'a00',
                name: 'A00铝',
                description: 'A00铝报价',
                icon: Coins,
                href: '/commodities',
              },
              {
                id: 'cci',
                name: 'CCI动力煤',
                description: 'CCI动力煤三档报价',
                icon: Coins,
                href: '/commodities?instrument=cci',
              },
            ]}
          />
        }
      >
        <div>
          <h1>{coal ? 'CCI动力煤' : 'A00铝'}</h1>
          <p>大宗商品报价</p>
        </div>
      </PageHeader>
      {coal ? <CCICoalPage /> : <A00Page />}
    </div>
  )
}
