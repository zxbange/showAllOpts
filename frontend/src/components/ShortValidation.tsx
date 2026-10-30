import { useEffect, useState } from 'react'

type Metric = Record<string, string | number | null>
type Job = {
  id: string
  methods: string[]
  start: string
  end: string
  observed: string
  version: string
  status: string
  stage: string
  done: number
  total: number
  captured: number
  updated_at: string
  error: string | null
  summary: Metric[]
  windows: Metric[]
}
const choices = { bukou: '补票龙', redhover: '红悬停', xuli: '蓄力龙', oversold: '超跌后止跌反弹' }
const statuses: Record<string, string> = {
  queued: '等待',
  running: '运行中',
  failed: '失败',
  cancelled: '已暂停',
  completed: '完成',
}
// getRandomValues also works on the existing HTTP deployment (randomUUID does not).
function requestToken() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
    value.toString(16).padStart(2, '0'),
  ).join('')
}
async function api(path = '', body?: unknown): Promise<unknown> {
  const response = await fetch(
    '/api/v1/short-term/validation/jobs' + path,
    body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
  )
  const data = await response.json()
  if (!response.ok)
    throw new Error(
      typeof data.detail === 'string' ? data.detail : '操作失败，请检查日期和策略选择',
    )
  return data
}
const metric = (value: string | number | null) =>
  value == null
    ? '—'
    : typeof value === 'number'
      ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
      : value
function Metrics({ rows }: { rows: Metric[] }) {
  if (!rows.length) return null
  const columns = Object.keys(rows[0])
  return (
    <div style={{ overflowX: 'auto' }}>
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c}>{metric(row[c])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
export function ShortValidation() {
  const [methods, setMethods] = useState<string[]>(Object.keys(choices))
  const [start, setStart] = useState('2024-01-01')
  const [end, setEnd] = useState('')
  const [observed, setObserved] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [requestId, setRequestId] = useState(() => requestToken())
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const data = (await api()) as { jobs: Job[] }
        if (!Array.isArray(data.jobs)) throw new Error('任务响应格式异常，请刷新后重试')
        if (active) {
          setJobs(data.jobs)
          setError('')
        }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : '任务读取失败')
      } finally {
        if (active) timer = setTimeout(poll, 3000)
      }
    }
    void poll()
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [])
  async function operate(path: string, body: unknown) {
    setBusy(true)
    setError('')
    try {
      const job = (await api(path, body)) as Job
      setJobs((old) => [job, ...old.filter((j) => j.id !== job.id)])
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="short-validation" aria-label="短线胜率验证">
      <h2>短线胜率验证</h2>
      <p>
        四策略统一沪深股票池、基础过滤、后复权价格与原始成交量。按 T 收盘比较 T+1/2/3
        交易日收盘，严格上涨计胜，持平计入分母；各期限仅计有效样本，停牌、缺行情及待观察单列。这是形态统计，非逐笔实盘回测，不含费用，不代表可成交收益。
      </p>
      <p>
        沿用原90日形态窗口；红悬停的价格取整与统一股票池可能使信号不同于生产选股。输入冻结后从本地断点计算，修复源数据后可准备重新读取数据并创建新任务；重试会复用已保存输入。
      </p>
      <fieldset>
        <legend>选择统计策略（可多选）</legend>
        {Object.entries(choices).map(([id, label]) => (
          <label key={id}>
            <input
              type="checkbox"
              checked={methods.includes(id)}
              onChange={(e) =>
                setMethods((old) => (e.target.checked ? [...old, id] : old.filter((m) => m !== id)))
              }
            />
            {label}
          </label>
        ))}
      </fieldset>
      <div className="short-controls">
        <label>
          信号开始
          <input
            aria-label="信号开始"
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          信号截止
          <input
            aria-label="信号截止"
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        <label>
          观察截止
          <input
            aria-label="观察截止"
            type="date"
            value={observed}
            onChange={(e) => setObserved(e.target.value)}
          />
        </label>
        <button
          className="button"
          disabled={
            busy ||
            !methods.length ||
            !start ||
            (!!end && start > end) ||
            (!!end && !!observed && end > observed)
          }
          onClick={() =>
            void operate('', {
              methods,
              start,
              end: end || null,
              observed: observed || null,
              request_id: requestId,
            })
          }
        >
          开始验证
        </button>
        <button
          className="button button-quiet"
          disabled={busy}
          onClick={() => {
            setRequestId(requestToken())
            setError('已准备新任务，再点击开始验证；现有结果保留。')
          }}
        >
          准备重新读取数据
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {!jobs.length && (
        <p>
          尚无验证任务。默认自2024-01-01开始；截止日期留空采用已有最新收盘交易日。服务器会检查已有研究数据和预热覆盖。
        </p>
      )}
      {jobs.map((job) => (
        <article key={job.id}>
          <h3>
            {job.methods.map((m) => choices[m as keyof typeof choices]).join(' · ')} —{' '}
            {statuses[job.status] || job.status}
          </h3>
          <p>
            {job.start} 至 {job.end || '最新可用日'} · 观察截至 {job.observed || '最新可用日'} ·{' '}
            {job.version}
          </p>
          <p role="status">
            {job.stage} · 输入 {job.captured}/{job.total} · 已计算 {job.done}/{job.total}
          </p>
          <p>
            最后活动：{new Date(job.updated_at).toLocaleString('zh-CN')}
            （查询或单股计算期间不会虚增进度）
          </p>
          {job.error && <p role="alert">{job.error}</p>}
          {['failed', 'cancelled'].includes(job.status) && (
            <button
              className="button"
              disabled={busy}
              onClick={() => void operate('/' + job.id + '/retry', {})}
            >
              从断点重试
            </button>
          )}
          {['queued', 'running'].includes(job.status) && (
            <button
              className="button button-quiet"
              disabled={busy}
              onClick={() => void operate('/' + job.id + '/cancel', {})}
            >
              暂停任务
            </button>
          )}
          {job.status === 'completed' && (
            <>
              {job.summary.some((row) => Number(row['有效样本']) < 30) && (
                <p>
                  部分策略/期限有效样本不足30，样本偏少，统计不稳定；“—”表示无有效样本，不能视为零胜率。
                </p>
              )}
              <Metrics rows={job.summary} />
              <Metrics rows={job.windows} />
            </>
          )}
        </article>
      ))}
    </section>
  )
}
