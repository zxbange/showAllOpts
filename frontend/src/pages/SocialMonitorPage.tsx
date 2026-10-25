import { useCallback, useEffect, useState } from 'react'
import {
  Bell,
  ExternalLink,
  Plus,
  RefreshCw,
  Send,
  MessageSquare,
  Users,
  Settings2,
  Search,
  Clock3,
} from 'lucide-react'
import '../styles/social.css'
import { SocialBrowserLogin } from '../components/SocialBrowserLogin'

type Platform = 'weibo' | 'xueqiu'
type View = 'feed' | 'authors' | 'settings'
type TelegramSetting = 'token' | 'chat_id'
const views = [
  { id: 'feed', label: '最新动态', icon: MessageSquare },
  { id: 'authors', label: '监控博主', icon: Users },
  { id: 'settings', label: '通知设置', icon: Settings2 },
] as const
type Author = {
  id: number
  platform: Platform
  uid: string
  name: string
  screen_name?: string | null
  enabled: number
  last_check: string | null
  last_error: string | null
  scan_page?: number
}
type PlatformState = {
  paused?: boolean
  reason?: string | null
  retry_at?: number
  alert_sent_at?: string | null
  paused_at?: string | null
  reason_code?: string | null
  http_status?: number | null
}
type Status = {
  browser_login_available?: boolean
  enabled: boolean
  interval_min: number
  interval_max: number
  last_check: string | null
  next_check: string | null
  last_error: string | null
  token_configured: boolean
  chat_id_configured: boolean
  weibo_cookie_configured: boolean
  xueqiu_cookie_configured: boolean
  authors: Author[]
  counts: Record<string, number>
  night_rest?: boolean
  platforms?: Partial<Record<Platform, PlatformState>>
}
type Post = {
  id: number
  platform: Platform
  author_name: string
  status: string
  last_error: string | null
  discovered_at: string
  sent_at: string | null
  payload: { name: string; text: string; time: string; url: string }
}
type Feed = { total: number; page: number; items: Post[] }
const label = (platform: Platform) => (platform === 'weibo' ? '微博' : '雪球')
const authorName = (author: Author) =>
  author.screen_name || (author.name !== author.uid ? author.name : '') || '待获取博主名称'
const statusLabels: Record<string, string> = {
  baseline: '历史记录',
  pending: '待推送',
  sent: '已推送',
}
function date(value: string | number | null) {
  if (!value) return '尚未检查'
  const moment = new Date(value)
  return Number.isNaN(moment.getTime())
    ? typeof value === 'string'
      ? value
      : '无法显示时间'
    : moment.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

function authorState(author: Author, status: Status) {
  if (!author.enabled)
    return { label: '已暂停', reason: '该博主已手动暂停，点击“恢复”重新监控。', waiting: false }
  if (!status.enabled)
    return { label: '已暂停', reason: '总监控开关已关闭，点击“启动监控”恢复。', waiting: false }
  const platform = status.platforms?.[author.platform]
  if (platform?.paused)
    return {
      label: '平台已暂停',
      reason:
        platform.reason ||
        author.last_error ||
        '平台已自动暂停，但旧记录未保存具体原因，请检查平台登录和验证状态。',
      waiting: false,
    }
  if (status.night_rest)
    return {
      label: '夜间休息中',
      reason: '北京时间 00:00～07:00 暂停抓取，07:00 自动恢复并补抓。',
      waiting: false,
    }
  if (platform?.retry_at && platform.retry_at * 1000 > Date.now())
    return {
      label: '退避等待中',
      reason: platform.reason || author.last_error || '平台请求暂时失败，正在等待自动重试。',
      waiting: true,
    }
  return { label: '监控中', reason: '', waiting: false }
}

export function SocialMonitorPage() {
  const [view, setView] = useState<View>('feed')
  const [status, setStatus] = useState<Status | null>(null)
  const [feed, setFeed] = useState<Feed>({ total: 0, page: 1, items: [] })
  const [page, setPage] = useState(1)
  const [authorFilter, setAuthorFilter] = useState('')
  const [platformFilter, setPlatformFilter] = useState('')
  const [keywordDraft, setKeywordDraft] = useState('')
  const [keyword, setKeyword] = useState('')
  const [platform, setPlatform] = useState<Platform>('weibo')
  const [url, setUrl] = useState('https://weibo.com/u/3962719063')
  const [name, setName] = useState('')
  const [secrets, setSecrets] = useState({
    token: '',
    chat_id: '',
  })
  const [editingSecrets, setEditingSecrets] = useState({ token: false, chat_id: false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const api = useCallback(async <T,>(path: string, body?: unknown): Promise<T> => {
    const response = await fetch(`/api/v1/social-monitor${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    const result = await response.json().catch(() => null)
    if (!response.ok) {
      throw new Error(
        typeof result?.detail === 'string' ? result.detail : `请求失败（${response.status}）`,
      )
    }
    return result as T
  }, [])

  const refresh = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page) })
    if (authorFilter) query.set('author_id', authorFilter)
    if (platformFilter) query.set('platform', platformFilter)
    if (keyword) query.set('keyword', keyword)
    const [nextStatus, nextFeed] = await Promise.all([
      api<Status>('/status'),
      api<Feed>(`/posts?${query}`),
    ])
    setStatus(nextStatus)
    setFeed(nextFeed)
  }, [api, page, authorFilter, platformFilter, keyword])

  useEffect(() => {
    let active = true
    const load = () => {
      if (active)
        void refresh().catch((e: Error) => {
          if (active) setError(e.message)
        })
    }
    load()
    const timer = window.setInterval(load, 10000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [refresh])

  async function action(work: () => Promise<void>) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await work()
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  function saveTelegramConfig(key?: TelegramSetting) {
    if (!status) return
    const changes = key
      ? { [key]: secrets[key].trim() }
      : Object.fromEntries(
          Object.entries(secrets).filter(
            ([field, value]) => !status[`${field as TelegramSetting}_configured`] && value.trim(),
          ),
        )
    if (!Object.values(changes).some(Boolean)) return
    void action(async () => {
      setStatus(await api<Status>('/config', changes))
      setSecrets((current) => ({
        ...current,
        ...Object.fromEntries(Object.keys(changes).map((field) => [field, ''])),
      }))
      setEditingSecrets((current) => ({
        ...current,
        ...Object.fromEntries(Object.keys(changes).map((field) => [field, false])),
      }))
      setNotice('配置已保存。')
    })
  }

  const activeAuthors = status?.authors.filter((author) => author.enabled) ?? []
  const allPlatformsPaused =
    activeAuthors.length > 0 &&
    activeAuthors.every((author) => status?.platforms?.[author.platform]?.paused)

  if (!status)
    return (
      <section className="social-page">
        <header className="social-heading">
          <h1>博主动态监控</h1>
        </header>
        <div className="social-panel" aria-busy={!error || busy}>
          {error ? (
            <>
              <p role="alert" className="social-error">
                {error}
              </p>
              <button
                className="button button-primary"
                disabled={busy}
                onClick={() => void action(refresh)}
              >
                重新加载
              </button>
            </>
          ) : (
            <p role="status" className="social-muted">
              正在加载博主动态…
            </p>
          )}
        </div>
      </section>
    )

  return (
    <section className="social-page">
      <header className="social-heading">
        <div>
          <h1>博主动态监控</h1>
        </div>
      </header>
      {error && (
        <div role="alert" className="social-error">
          {error}
        </div>
      )}
      {notice && (
        <div role="status" className="social-notice">
          {notice}
        </div>
      )}
      <div className="social-overview">
        <div className="social-summary">
          <div>
            <span>监控状态</span>
            <strong className={status.enabled ? 'social-live' : ''}>
              {status.enabled
                ? allPlatformsPaused
                  ? '等待人工处理'
                  : status.night_rest
                    ? '夜间休息中'
                    : '后台监控中'
                : '已暂停'}
            </strong>
          </div>
          <div>
            <span>已启用博主 / 全部</span>
            <strong>
              {status.authors.filter((a) => a.enabled).length}{' '}
              <small>/ {status.authors.length}</small>
            </strong>
          </div>
          <div>
            <span>已推送 / 待推送</span>
            <strong>
              {status.counts.sent || 0} <small>/ {status.counts.pending || 0}</small>
            </strong>
          </div>
          <div>
            <span>随机检查间隔</span>
            <strong>
              {status.interval_min / 60}–{status.interval_max / 60} <small>分钟</small>
            </strong>
          </div>
        </div>
        <div className="social-schedule">
          <div className="social-schedule-details">
            <div className="social-check-times">
              <span>最近检查：{date(status.last_check)}</span>
              <span>
                下次检查：
                {status.enabled
                  ? status.next_check
                    ? date(status.next_check)
                    : '即将开始'
                  : '已暂停'}
              </span>
            </div>
            <p className="social-rule">
              <Clock3 size={14} />
              北京时间 00:00～07:00 休息，早上自动补抓；帖子保留最近一年。
            </p>
          </div>
          <button
            disabled={busy}
            className="button button-primary"
            onClick={() =>
              void action(async () => {
                setStatus(await api<Status>('/config', { enabled: !status.enabled }))
              })
            }
          >
            <Bell size={16} />
            {status.enabled ? '暂停全部监控' : '启动监控'}
          </button>
        </div>
        {!status.enabled && (
          <p className="social-state-note">
            <strong>暂停原因：</strong>总监控开关已关闭，点击“启动监控”开始采集。
          </p>
        )}
        {status.enabled && status.night_rest && !allPlatformsPaused && (
          <p className="social-state-note">
            <strong>休息原因：</strong>当前为北京时间夜间休息时段，07:00 自动恢复，无需手动操作。
          </p>
        )}
      </div>
      {status.last_error && (
        <div className="social-error" role="alert">
          {status.last_error}
        </div>
      )}
      {(['weibo', 'xueqiu'] as Platform[]).map((platform) => {
        const state = status.platforms?.[platform]
        if (!state?.paused && !(state?.retry_at && state.retry_at * 1000 > Date.now())) return null
        return (
          <div key={platform} className="social-panel social-intervention">
            <h2>
              {label(platform)}：{state.paused ? '已自动暂停，等待人工处理' : '退避等待中'}
            </h2>
            <dl className="social-pause-details">
              <div>
                <dt>{state.paused ? '暂停原因' : '暂缓原因'}</dt>
                <dd>
                  {state.reason ||
                    status.authors.find(
                      (author) => author.platform === platform && author.last_error,
                    )?.last_error ||
                    '旧记录未保存具体原因，请在浏览器检查平台登录、访问权限或安全验证提示。'}
                </dd>
              </div>
              {state.http_status && (
                <div>
                  <dt>平台状态码</dt>
                  <dd>HTTP {state.http_status}</dd>
                </div>
              )}
              {state.paused && state.paused_at && (
                <div>
                  <dt>暂停时间</dt>
                  <dd>{date(state.paused_at)}</dd>
                </div>
              )}
              {!state.paused && state.retry_at && (
                <div>
                  <dt>最早重试</dt>
                  <dd>{date(state.retry_at * 1000)}</dd>
                </div>
              )}
            </dl>
            {state.paused && (
              <>
                <p className="social-muted">
                  {state.alert_sent_at
                    ? '提醒已发送，本次暂停不再重复提醒。'
                    : '提醒待发送，失败将自动重试。'}
                  请在通知设置中重新扫码登录，完成平台验证后再恢复。
                </p>
                <button
                  disabled={busy}
                  className="button"
                  onClick={() =>
                    void action(async () => {
                      setStatus(await api<Status>(`/platforms/${platform}/resume`, {}))
                      setNotice(`${label(platform)}已人工恢复；夜间将在 07:00 开始检查。`)
                    })
                  }
                >
                  人工处理完成，恢复{label(platform)}
                </button>
              </>
            )}
          </div>
        )
      })}
      <div
        className="social-tabs"
        role="tablist"
        aria-label="博主监控分区"
        onKeyDown={(event) => {
          const index = views.findIndex((item) => item.id === view)
          const next =
            event.key === 'ArrowRight'
              ? (index + 1) % views.length
              : event.key === 'ArrowLeft'
                ? (index + views.length - 1) % views.length
                : event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? views.length - 1
                    : -1
          if (next < 0) return
          event.preventDefault()
          setView(views[next].id)
          document.getElementById(`social-tab-${views[next].id}`)?.focus()
        }}
      >
        {views.map((item) => (
          <button
            key={item.id}
            id={`social-tab-${item.id}`}
            role="tab"
            aria-selected={view === item.id}
            aria-controls={`social-panel-${item.id}`}
            tabIndex={view === item.id ? 0 : -1}
            onClick={() => setView(item.id)}
          >
            <item.icon size={17} />
            <span>{item.label}</span>
            {item.id === 'authors' && <small>{status.authors.length}</small>}
          </button>
        ))}
      </div>
      <div className="social-management" hidden={view === 'feed'}>
        <section
          className="social-panel"
          hidden={view !== 'authors'}
          role="tabpanel"
          id="social-panel-authors"
          aria-labelledby="social-tab-authors"
        >
          <div className="social-panel-heading">
            <div>
              <h2>监控用户</h2>
              <p className="social-muted">添加关注的博主，统一管理监控状态。</p>
            </div>
            <span className="social-count">共 {status.authors.length} 位</span>
          </div>
          <form
            className="social-author-form"
            onSubmit={(event) => {
              event.preventDefault()
              void action(async () => {
                setStatus(await api<Status>('/authors', { platform, url, name }))
                setUrl('')
                setName('')
                setNotice('已添加，下一轮检查开始采集；首次历史帖子仅展示。')
              })
            }}
          >
            <label>
              平台
              <select value={platform} onChange={(e) => setPlatform(e.target.value as Platform)}>
                <option value="weibo">微博</option>
                <option value="xueqiu">雪球</option>
              </select>
            </label>
            <label>
              博主主页或用户编号
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="粘贴主页链接或输入用户编号"
                required
                maxLength={500}
              />
            </label>
            <label>
              备注名称
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="可选"
                maxLength={100}
              />
            </label>
            <button className="button button-primary" disabled={busy}>
              <Plus size={16} />
              添加用户
            </button>
          </form>
          <div className="social-authors">
            {!status.authors.length && (
              <p className="social-muted">还没有监控用户，先添加一个主页。</p>
            )}
            {status.authors.map((author) => {
              const monitoring = authorState(author, status)
              return (
                <article className="social-author" key={author.id}>
                  <div>
                    <span className={`social-platform ${author.platform}`}>
                      {label(author.platform)}
                    </span>
                    <strong title={authorName(author)}>{authorName(author)}</strong>
                  </div>
                  <p className="social-muted">
                    {monitoring.label} · 最近检查 {date(author.last_check)}
                    {author.scan_page && author.scan_page > 1
                      ? ` · 待继续补抓第 ${author.scan_page} 页`
                      : ''}
                  </p>
                  {monitoring.reason && (
                    <p className="social-author-reason">
                      <strong>{monitoring.waiting ? '暂缓原因：' : '暂停原因：'}</strong>
                      {monitoring.reason}
                    </p>
                  )}
                  {author.last_error && author.last_error !== monitoring.reason && (
                    <p className="social-error">最近抓取错误：{author.last_error}</p>
                  )}
                  <div className="social-actions">
                    <button
                      disabled={busy}
                      className="button"
                      onClick={() =>
                        void action(async () => {
                          setStatus(
                            await api<Status>(`/authors/${author.id}`, {
                              enabled: !author.enabled,
                            }),
                          )
                        })
                      }
                    >
                      {author.enabled ? '暂停' : '恢复'}
                    </button>
                    <button
                      disabled={busy}
                      className="button"
                      onClick={() => {
                        if (window.confirm(`删除 ${authorName(author)} 的监控及已采集帖子？`))
                          void action(async () => {
                            setStatus(await api<Status>(`/authors/${author.id}/delete`, {}))
                            setAuthorFilter('')
                            setPage(1)
                            await refresh()
                          })
                      }}
                    >
                      删除
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        </section>
        <section
          className="social-panel"
          hidden={view !== 'settings'}
          role="tabpanel"
          id="social-panel-settings"
          aria-labelledby="social-tab-settings"
        >
          <div className="social-panel-heading">
            <div>
              <h2>通知与平台登录</h2>
            </div>
            <Settings2 size={20} />
          </div>
          {view === 'settings' && (
            <SocialBrowserLogin
              available={!!status.browser_login_available}
              onSaved={() => {
                void refresh().catch((err: Error) => setError(err.message))
              }}
            />
          )}
          <h3>Telegram 通知配置</h3>
          <div className="social-form">
            <div className="social-field-grid">
              {(
                [
                  ['token', '机器人密钥', status.token_configured],
                  ['chat_id', '接收会话编号', status.chat_id_configured],
                ] as const
              ).map(([key, title, configured]) => (
                <form
                  className="social-credential-field"
                  key={key}
                  onSubmit={(event) => {
                    event.preventDefault()
                    saveTelegramConfig(configured ? key : undefined)
                  }}
                >
                  <div className="social-label-row">
                    <span>{title}</span>
                    <span className={`social-config-state ${configured ? 'configured' : ''}`}>
                      {configured ? '已配置' : '未配置'}
                    </span>
                  </div>
                  {configured && !editingSecrets[key] ? (
                    <div
                      className="social-credential-saved"
                      role="group"
                      aria-label={`${title}已配置`}
                    >
                      <span className="social-secret-mask" aria-hidden="true">
                        ***
                      </span>
                      <button
                        type="button"
                        className="button"
                        aria-label={`修改${title}`}
                        disabled={busy}
                        onClick={() => {
                          setSecrets((current) => ({ ...current, [key]: '' }))
                          setEditingSecrets((current) => ({ ...current, [key]: true }))
                        }}
                      >
                        修改
                      </button>
                    </div>
                  ) : (
                    <input
                      type="password"
                      aria-label={title}
                      autoComplete="off"
                      disabled={busy}
                      value={secrets[key]}
                      onChange={(e) =>
                        setSecrets((current) => ({ ...current, [key]: e.target.value }))
                      }
                      placeholder={configured ? `填写新的${title}` : `填写 ${title}`}
                      maxLength={key === 'token' ? 300 : 100}
                    />
                  )}
                  {configured && editingSecrets[key] && (
                    <div className="social-actions">
                      <button
                        disabled={busy || !secrets[key].trim()}
                        className="button button-primary"
                        aria-label={`保存${title}`}
                      >
                        保存
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        className="button"
                        aria-label={`取消修改${title}`}
                        onClick={() => {
                          setSecrets((current) => ({ ...current, [key]: '' }))
                          setEditingSecrets((current) => ({ ...current, [key]: false }))
                        }}
                      >
                        取消
                      </button>
                    </div>
                  )}
                </form>
              ))}
            </div>
            <div className="social-actions">
              {(!status.token_configured || !status.chat_id_configured) && (
                <button
                  type="button"
                  disabled={
                    busy ||
                    !(
                      (!status.token_configured && secrets.token.trim()) ||
                      (!status.chat_id_configured && secrets.chat_id.trim())
                    )
                  }
                  className="button button-primary"
                  onClick={() => saveTelegramConfig()}
                >
                  保存配置
                </button>
              )}
              <button
                type="button"
                disabled={busy || !status.token_configured || !status.chat_id_configured}
                className="button"
                onClick={() =>
                  void action(async () => {
                    const result = await api<{ message: string }>('/telegram-test', {})
                    setNotice(result.message)
                  })
                }
              >
                <Send size={15} />
                发送测试消息
              </button>
            </div>
          </div>
        </section>
      </div>
      <section
        className="social-panel social-feed"
        hidden={view !== 'feed'}
        role="tabpanel"
        id="social-panel-feed"
        aria-labelledby="social-tab-feed"
      >
        <div className="social-feed-heading">
          <div>
            <h2>最新动态</h2>
            <p className="social-muted">共 {feed.total} 条动态 · 北京时间 · 自动更新</p>
          </div>
          <div className="social-actions">
            <select
              aria-label="按平台筛选"
              value={platformFilter}
              onChange={(e) => {
                setPlatformFilter(e.target.value)
                setPage(1)
              }}
            >
              <option value="">全部平台</option>
              <option value="weibo">微博</option>
              <option value="xueqiu">雪球</option>
            </select>
            <select
              aria-label="按用户筛选"
              value={authorFilter}
              onChange={(e) => {
                setAuthorFilter(e.target.value)
                setPage(1)
              }}
            >
              <option value="">全部用户</option>
              {status.authors.map((a) => (
                <option key={a.id} value={a.id}>
                  {authorName(a)}
                </option>
              ))}
            </select>
            <button disabled={busy} className="button" onClick={() => void action(refresh)}>
              <RefreshCw size={15} />
              刷新列表
            </button>
          </div>
        </div>
        <form
          className="social-search social-actions"
          onSubmit={(event) => {
            event.preventDefault()
            setKeyword(keywordDraft.trim())
            setPage(1)
          }}
        >
          <div className="social-search-field">
            <Search size={17} />
            <input
              aria-label="搜索帖子内容"
              placeholder="输入关键词检索全部已抓取帖子"
              value={keywordDraft}
              onChange={(event) => setKeywordDraft(event.target.value)}
              maxLength={200}
            />
          </div>
          <button className="button button-primary" disabled={busy}>
            搜索
          </button>
          {keyword && (
            <button
              type="button"
              className="button"
              onClick={() => {
                setKeyword('')
                setKeywordDraft('')
                setPage(1)
              }}
            >
              清除搜索
            </button>
          )}
        </form>
        {!feed.items.length && (
          <div className="social-empty">
            <Bell size={28} />
            <h3>{keyword ? '没有匹配的帖子' : '等待博主动态'}</h3>
            <p>
              {keyword
                ? '请更换关键词，或调整平台、用户筛选条件。'
                : '配置 Telegram、添加用户并启动监控后，采集内容会显示在这里。'}
            </p>
            {!keyword && !status.authors.length && (
              <button className="button button-primary" onClick={() => setView('authors')}>
                添加第一位博主
              </button>
            )}
          </div>
        )}
        {feed.items.map((post) => (
          <article className="social-post" key={post.id}>
            <header>
              <span className={`social-post-avatar ${post.platform}`}>
                {post.author_name.slice(0, 1)}
              </span>
              <span className={`social-platform ${post.platform}`}>{label(post.platform)}</span>
              <strong>{post.author_name}</strong>
              <span className={`social-post-status ${post.status}`}>
                {statusLabels[post.status] || post.status}
              </span>
            </header>
            <p className="social-muted">
              <time>发布时间：{date(post.payload.time)}</time>
              {post.sent_at && <> · 推送时间：{date(post.sent_at)}</>}
            </p>
            <div className="social-post-text">{post.payload.text}</div>
            {post.last_error && <p className="social-error">{post.last_error}</p>}
            <a className="social-original" href={post.payload.url} target="_blank" rel="noreferrer">
              查看原文 <ExternalLink size={14} />
            </a>
          </article>
        ))}
        <div className="social-pagination">
          <button disabled={busy || page <= 1} className="button" onClick={() => setPage(page - 1)}>
            上一页
          </button>
          <span>
            第 {page} / {Math.max(1, Math.ceil(feed.total / 20))} 页
          </span>
          <button
            disabled={busy || page * 20 >= feed.total}
            className="button"
            onClick={() => setPage(page + 1)}
          >
            下一页
          </button>
        </div>
      </section>
    </section>
  )
}
