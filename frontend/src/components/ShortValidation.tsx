import { useEffect, useState, useRef } from 'react'
import { ShieldCheck, LockKeyhole, Play, Database, SlidersHorizontal, History } from 'lucide-react'
import { ValidationResults } from './ValidationResults'
import { ValidationDeleteDialog } from './ValidationDeleteDialog'
import { validationRequest, type ValidationAuth } from '../api/validation'
import {
  ValidationSourceEditor,
  type SourceRef,
  type SourceEditorHandle,
} from './ValidationSourceEditor'

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
  source?: SourceRef | null
  cancel_requested_at?: string
  cancelled_at?: string
  source_name?: string
  input_mode?: string
  stage_started_at?: string
  timings?: Record<string, number>
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
  paused: '已暂停',
  cancelling: '正在取消',
  cancelled: '已取消',
  completed: '完成',
}
// getRandomValues also works on the existing HTTP deployment (randomUUID does not).
function requestToken() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
    value.toString(16).padStart(2, '0'),
  ).join('')
}
export function ShortValidation() {
  const [key, setKey] = useState('')
  const [unlocked, setUnlocked] = useState<ValidationAuth | null>(null)
  const [mode, setMode] = useState<'pair' | 'legacy' | null>(null)
  const [message, setMessage] = useState('')
  const [allowHttp, setAllowHttp] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const pairing = useRef(false)
  const [pairBusy, setPairBusy] = useState(false)
  const usingHttp = window.location.protocol === 'http:'
  const transportAllowed =
    window.location.protocol === 'https:' ||
    (usingHttp && (mode === 'pair' ? confirmed : allowHttp))
  useEffect(() => {
    let active = true
    fetch('/api/v1/short-term/validation/access', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'X-Validation-Origin': window.location.origin },
      redirect: 'error',
    })
      .then((r) => {
        if (!r.ok) throw new Error('无法读取验证管理状态')
        return r.json()
      })
      .then((data) => {
        if (!active) return
        setMode(data.mode === 'pair' ? 'pair' : 'legacy')
        setAllowHttp(data.https_required === false)
        if (data.authenticated && typeof data.csrf_token === 'string') {
          setUnlocked({ csrf: data.csrf_token })
          setConfirmed(true)
        }
      })
      .catch(() => {
        if (active) setMessage('无法读取验证管理状态，请刷新后重试')
      })
    const expired = () => {
      setUnlocked(null)
      setKey('')
      setMessage('会话已过期，请在终端领取新码并重新配对。')
    }
    window.addEventListener('validation-session-expired', expired)
    return () => {
      active = false
      window.removeEventListener('validation-session-expired', expired)
    }
  }, [])
  if (unlocked)
    return (
      <>
        <ValidationPanel
          adminKey={unlocked}
          onLock={async () => {
            try {
              if (typeof unlocked !== 'string') await validationRequest(unlocked, '/logout', {})
              setUnlocked(null)
              setKey('')
              setMessage('已注销；再次使用或更换浏览器需重新领取配对码。')
            } catch (e) {
              setMessage(e instanceof Error ? e.message : '注销失败，请重试')
            }
          }}
        />
        {message && <p role="alert">{message}</p>}
      </>
    )
  return (
    <section className="short-validation validation-pair-card" aria-label="验证管理登录">
      <div className="validation-pair-heading">
        <span className="validation-icon">
          <ShieldCheck size={25} aria-hidden="true" />
        </span>
        <span className="validation-eyebrow">站主验证 · 一次配对</span>
      </div>
      <h2>
        {mode === 'pair'
          ? '连接你的研究工作台'
          : mode === 'legacy'
            ? '解锁验证管理'
            : '正在读取访问状态'}
      </h2>
      {mode === 'pair' ? (
        <>
          <p>
            站主在服务器项目目录、使用API服务账户运行以下命令，领取10分钟有效的一次性配对码。无需编辑服务器配置。
          </p>
          <div className="validation-command">
            <span>01 · 在服务器终端领取配对码</span>
            <pre>
              APP_ENV_FILE=config/app.config backend/.venv/bin/python backend/validation_pair.py
              --origin {window.location.origin}
            </pre>
          </div>
          <details className="validation-disclosure">
            <summary>会话有效期与重新配对</summary>
            <p>
              命令使用现有部署的配置路径。配对码不要发送到聊天；会话最多8小时，刷新后保留。注销、过期或更换浏览器时重新领取；终端加
              --revoke-sessions 可撤销所有旧会话。
            </p>
          </details>
        </>
      ) : mode === 'legacy' ? (
        <p>当前沿用既有管理密钥模式，不能通过配对码绕过。管理密钥仅保留在页面内存。</p>
      ) : (
        <p className="validation-hint">正在确认会话，请稍候。</p>
      )}
      {usingHttp && mode === 'pair' && (
        <label className="validation-consent">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          我确认在HTTP上由本人操作并接受明文传输风险
        </label>
      )}
      {!transportAllowed && mode === 'legacy' && (
        <p role="alert">请通过HTTPS访问后再输入管理密钥或数据库凭证。</p>
      )}
      <div className="validation-pair-action">
        <label>
          {mode === 'pair' ? '一次性配对码' : '验证管理密钥'}
          <input
            aria-label={mode === 'pair' ? '一次性配对码' : '验证管理密钥'}
            type="password"
            autoComplete="off"
            disabled={!mode || !transportAllowed}
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
        </label>
        <button
          className="button button-primary"
          disabled={pairBusy || !key || !transportAllowed || !mode}
          onClick={async () => {
            if (pairing.current) return
            pairing.current = true
            setPairBusy(true)
            try {
              if (mode === 'pair') {
                const result = await validationRequest<{ csrf_token: string }>(
                  { csrf: '' },
                  '/pair',
                  { code: key, http_confirmed: confirmed },
                )
                setUnlocked({ csrf: result.csrf_token })
              } else {
                await validationRequest(key, '/jobs')
                setUnlocked(key)
              }
              setKey('')
              setMessage('')
            } catch (e) {
              setKey('')
              setMessage(e instanceof Error ? e.message : '验证失败')
            } finally {
              pairing.current = false
              setPairBusy(false)
            }
          }}
        >
          <LockKeyhole size={16} aria-hidden="true" />
          {pairBusy ? '正在确认…' : mode === 'pair' ? '配对并进入' : '解锁验证管理'}
        </button>
      </div>
      {message && (
        <p className="validation-feedback" role="alert">
          {message}
        </p>
      )}
    </section>
  )
}
function ValidationPanel({ adminKey, onLock }: { adminKey: ValidationAuth; onLock: () => void }) {
  const sourceEditor = useRef<SourceEditorHandle>(null)
  const starting = useRef(false)
  const firstPoll = useRef(true)
  const [setupOpen, setSetupOpen] = useState(true)
  const [source, setSource] = useState<SourceRef | null>(null)
  const [methods, setMethods] = useState<string[]>(Object.keys(choices))
  const [start, setStart] = useState('2024-01-01')
  const [end, setEnd] = useState('')
  const [observed, setObserved] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [deleting, setDeleting] = useState<Job | null>(null)
  const deletedIds = useRef(new Set<string>())
  const historyHeading = useRef<HTMLHeadingElement>(null)
  const deleteTrigger = useRef<HTMLButtonElement | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [requestId, setRequestId] = useState(() => requestToken())
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const data = await validationRequest<{ jobs: Job[] }>(adminKey, '/jobs')
        if (!Array.isArray(data.jobs)) throw new Error('任务响应格式异常，请刷新后重试')
        if (active) {
          // Ignore a GET begun before a successful deletion returned.
          setJobs(data.jobs.filter((job) => !deletedIds.current.has(job.id)))
          if (firstPoll.current) {
            if (data.jobs.length) setSetupOpen(false)
            firstPoll.current = false
          }
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
  }, [adminKey])
  async function operate(path: string, body: unknown) {
    setBusy(true)
    setError('')
    try {
      const job = await validationRequest<Job>(adminKey, '/jobs' + path, body)
      if (!deletedIds.current.has(job.id))
        setJobs((old) => [job, ...old.filter((j) => j.id !== job.id)])
      if (!path) setSetupOpen(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="short-validation validation-workspace" aria-label="短线胜率验证">
      <div className="validation-section-heading">
        <div>
          <span className="validation-eyebrow">研究设置</span>
          <h2>新建验证</h2>
        </div>
        <button className="button button-quiet" onClick={onLock}>
          <LockKeyhole size={15} aria-hidden="true" />
          锁定验证管理
        </button>
      </div>
      <details
        className="validation-setup"
        open={setupOpen}
        onToggle={(e) => setSetupOpen(e.currentTarget.open)}
      >
        <summary>
          配置新的验证任务<span>数据库 · 策略 · 日期</span>
        </summary>
        <div className="validation-setup-grid">
          <div className="validation-card">
            <div className="validation-card-heading">
              <Database size={19} aria-hidden="true" />
              <h3>01 数据库连接</h3>
              <span>后台加密保存</span>
            </div>
            <ValidationSourceEditor
              ref={sourceEditor}
              adminKey={adminKey}
              selected={source}
              onSelect={setSource}
            />
          </div>
          <div className="validation-card validation-parameters">
            <div className="validation-card-heading">
              <SlidersHorizontal size={19} aria-hidden="true" />
              <h3>02 策略与日期</h3>
            </div>
            <details className="validation-disclosure validation-methodology">
              <summary>统计口径与数据说明</summary>
              <p>
                四策略统一沪深股票池、基础过滤、后复权价格与原始成交量。按 T 收盘比较 T+1/2/3
                交易日收盘，严格上涨计胜，持平计入分母；各期限仅计有效样本，停牌、缺行情及待观察单列。这是形态统计，非逐笔实盘回测，不含费用，不代表可成交收益。
              </p>
              <p>
                沿用原90日形态窗口；红悬停的价格取整与统一股票池可能使信号不同于生产选股。新任务按静态源分批读取、边算边提交，不再预先复制全部行情。重试跳过已提交股票，重读未提交部分；任务期间请保持源历史数据不变，若历史数据修订请新建任务。旧冻结任务仍复用已保存输入。
              </p>
            </details>
            <fieldset className="validation-strategies">
              <legend>选择统计策略（可多选）</legend>
              {Object.entries(choices).map(([id, label]) => (
                <label key={id} className={methods.includes(id) ? 'is-selected' : undefined}>
                  <input
                    type="checkbox"
                    aria-label={label}
                    checked={methods.includes(id)}
                    onChange={(e) =>
                      setMethods((old) =>
                        e.target.checked ? [...old, id] : old.filter((m) => m !== id),
                      )
                    }
                  />
                  <span>
                    {label}
                    <small>
                      {
                        (
                          {
                            bukou: '趋势回踩再起',
                            redhover: '涨停后的承接',
                            xuli: '缩量后放量突破',
                            oversold: '超跌后的收盘确认',
                          } as Record<string, string>
                        )[id]
                      }
                    </small>
                  </span>
                </label>
              ))}
            </fieldset>
            <div className="validation-dates">
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
            </div>
            <p className="validation-hint">截止日期留空，使用已采集的最新收盘交易日。</p>
            <div className="validation-start">
              <button
                className="button button-primary"
                disabled={
                  busy ||
                  !methods.length ||
                  !start ||
                  (!!end && start > end) ||
                  (!!end && !!observed && end > observed)
                }
                onClick={async () => {
                  if (starting.current) return
                  starting.current = true
                  setBusy(true)
                  try {
                    const prepared = await sourceEditor.current?.prepare()
                    await operate('', {
                      methods,
                      start,
                      end: end || null,
                      observed: observed || null,
                      request_id: requestId,
                      source: prepared ?? source,
                    })
                  } catch (e) {
                    setError(e instanceof Error ? e.message : '数据库配置保存失败')
                  } finally {
                    starting.current = false
                    setBusy(false)
                  }
                }}
              >
                <Play size={17} aria-hidden="true" />
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
            <p className="validation-hint">
              新任务每批最多20股，边读边算并保存断点，不预先冻结全部行情。任务期间请保持源历史数据不变。启动时自动保存连接，关闭页面后后台继续运行。
            </p>
          </div>
        </div>
      </details>
      {error && (
        <p className="validation-feedback" role="alert">
          {error}
        </p>
      )}
      <div className="validation-section-heading validation-history-heading">
        <div>
          <span className="validation-eyebrow">任务与结果</span>
          <h2 ref={historyHeading} tabIndex={-1}>
            <History size={21} aria-hidden="true" />
            验证记录
          </h2>
        </div>
        <span className="validation-hint">最近 {jobs.length} 个任务</span>
      </div>
      {!jobs.length && (
        <div className="validation-empty">
          <History size={28} aria-hidden="true" />
          <h3>从一次验证开始</h3>
          <p>
            尚无验证任务。默认自2024-01-01开始；截止日期留空采用已有最新收盘交易日。服务器会检查已有研究数据和预热覆盖。
          </p>
        </div>
      )}
      {jobs.map((job) => (
        <article className="validation-job" key={job.id} data-status={job.status}>
          <div className="validation-job-heading">
            <h3>
              {job.methods.map((m) => choices[m as keyof typeof choices]).join(' · ')} —{' '}
              <span className={'validation-status status-' + job.status}>
                {statuses[job.status] || job.status}
              </span>
            </h3>
          </div>
          <p className="validation-hint">
            {job.start} 至 {job.end || '最新可用日'} · 观察截至 {job.observed || '最新可用日'}
          </p>
          {job.status !== 'completed' && (
            <div className="validation-progress">
              <p role="status">
                {job.stage.replace(/；?在途工作返回后丢弃未提交部分/g, '')} ·{' '}
                {job.input_mode === 'static-batch-v1'
                  ? `已提交 ${job.done}/${job.total}`
                  : `输入 ${job.captured}/${job.total} · 已计算 ${job.done}/${job.total}`}
              </p>
              <div className="validation-progress-grid">
                {(job.input_mode === 'static-batch-v1'
                  ? [['已提交股票', job.done]]
                  : [
                      ['冻结输入', job.captured],
                      ['信号计算', job.done],
                    ]
                ).map(([label, value]) => (
                  <label key={label}>
                    {label}
                    <span>
                      {value} / {job.total}
                    </span>
                    <progress
                      aria-label={String(label)}
                      max={Math.max(1, job.total)}
                      value={Number(value)}
                    />
                  </label>
                ))}
              </div>
            </div>
          )}
          {job.error && (
            <p className="validation-feedback" role="alert">
              {job.error}
            </p>
          )}
          <div className="validation-job-actions">
            {['failed', 'paused'].includes(job.status) && (
              <button
                className="button"
                disabled={busy}
                onClick={() => void operate('/' + job.id + '/retry', {})}
              >
                重试
              </button>
            )}
            {['queued', 'running'].includes(job.status) && (
              <button
                className="button button-quiet"
                disabled={busy}
                onClick={() => void operate('/' + job.id + '/pause', {})}
              >
                暂停任务
              </button>
            )}
            {['queued', 'running', 'paused', 'failed'].includes(job.status) && (
              <button
                className="button button-quiet"
                disabled={busy}
                onClick={() => void operate('/' + job.id + '/cancel', {})}
              >
                取消任务
              </button>
            )}
            <button
              className="button button-quiet validation-delete-button"
              disabled={busy || ['running', 'cancelling'].includes(job.status)}
              onClick={(event) => {
                deleteTrigger.current = event.currentTarget
                setDeleting(job)
              }}
            >
              删除记录
            </button>
          </div>
          {['running', 'cancelling'].includes(job.status) && (
            <p className="validation-hint">删除前请先取消任务，并等待显示“已取消”。</p>
          )}
          {job.cancel_requested_at && (
            <p>
              请求取消：{new Date(job.cancel_requested_at).toLocaleString('zh-CN')}
              {job.cancelled_at
                ? ' · 已确认停止：' + new Date(job.cancelled_at).toLocaleString('zh-CN')
                : ' · 等待在途工作退出'}
            </p>
          )}
          <details className="validation-disclosure validation-task-meta">
            <summary>任务参数</summary>
            {job.stage_started_at && (
              <p>阶段开始：{new Date(job.stage_started_at).toLocaleString('zh-CN')}</p>
            )}
            {job.timings && (
              <p>
                最近阶段耗时（秒，非累计）：
                {Object.entries(job.timings)
                  .map(
                    ([phase, seconds]) =>
                      `${({ connect: '连接', ultra_research_bar: '行情读取', ultra_research_universe: '快照读取', compute: '单股计算', commit: '断点提交' } as Record<string, string>)[phase] || phase} ${seconds.toFixed(3)}`,
                  )
                  .join(' · ')}
              </p>
            )}
            <p>
              {job.input_mode === 'static-batch-v1'
                ? `已提交 ${job.done}/${job.total}`
                : `输入 ${job.captured}/${job.total} · 已计算 ${job.done}/${job.total}`}
            </p>
            <p>
              数据库：
              {job.source_name ||
                (job.source?.id === 'service' ? '服务默认数据库' : '已保存数据库')}
            </p>
          </details>
          {job.status === 'completed' && (
            <>
              {job.summary.some((row) => Number(row['有效样本']) < 30) && (
                <p>
                  部分策略/期限有效样本不足30，样本偏少，统计不稳定；“—”表示无有效样本，不能视为零胜率。
                </p>
              )}
              <ValidationResults summary={job.summary} windows={job.windows} />
            </>
          )}
        </article>
      ))}
      {deleting && (
        <ValidationDeleteDialog
          id={deleting.id}
          description={`${deleting.methods.map((m) => choices[m as keyof typeof choices]).join(' · ')} · ${deleting.start} 至 ${deleting.end || '最新可用日'}`}
          adminKey={adminKey}
          onBusy={setBusy}
          onClose={() => {
            setDeleting(null)
            requestAnimationFrame(() => {
              if (deleteTrigger.current?.isConnected && !deleteTrigger.current.disabled)
                deleteTrigger.current.focus()
              else historyHeading.current?.focus()
            })
          }}
          onDeleted={() => {
            deletedIds.current.add(deleting.id)
            setJobs((old) => old.filter((job) => job.id !== deleting.id))
            setDeleting(null)
            requestAnimationFrame(() => historyHeading.current?.focus())
          }}
        />
      )}
    </section>
  )
}
