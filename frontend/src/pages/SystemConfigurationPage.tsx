import { SystemRuntimeConfiguration } from '../components/SystemRuntimeConfiguration'
import { useEffect, useState } from 'react'
import { useAuth } from '../components/AccountAccess'
import { SystemNotificationConfiguration } from '../components/SystemNotificationConfiguration'
import { systemConfigurationRequest } from '../api/systemConfiguration'
import type { SystemConfiguration, TaskConfiguration } from '../api/systemConfiguration'
import '../styles/system-configuration.css'
const names: Record<string, string> = {
  cache: '本地数据同步',
  margin: '两融担保比例',
  etf: '国家队动态',
  commodities: 'A00 铝',
  growth_dividend: '创红比 / 科创红利比',
  market: '市场热度 / 抱团拥挤度',
  valuation_ruler: '估值标尺',
  cci: 'CCI 动力煤',
  cash_cow: '奶牛股',
  holdings: '个人持仓',
  social: '微博 / 雪球动态',
  social_cubes: '雪球组合',
}
export function SystemConfigurationPage() {
  return useAuth().session?.user?.role === 'admin' ? (
    <Editor />
  ) : (
    <section className="empty-state">
      <h1>系统配置</h1>
      <p>仅管理员可访问</p>
    </section>
  )
}
function Editor() {
  const [config, setConfig] = useState<SystemConfiguration | null>(null),
    [password, setPassword] = useState(''),
    [passwordEdited, setPasswordEdited] = useState(false),
    [integrationDraft, setIntegrationDraft] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(''),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [reload, setReload] = useState(0)
  useEffect(() => {
    const c = new AbortController()
    systemConfigurationRequest<SystemConfiguration>('GET', undefined, false, c.signal)
      .then(setConfig)
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message)
      })
    return () => c.abort()
  }, [reload])
  function updateTask(key: string, patch: Partial<TaskConfiguration>) {
    setConfig((value) =>
      value
        ? {
            ...value,
            schedules: { ...value.schedules, [key]: { ...value.schedules[key], ...patch } },
          }
        : value,
    )
  }
  async function submit(test = false) {
    if (!config || busy) return
    setBusy(test ? 'test' : 'save')
    setError('')
    setNotice('')
    const { password_set: _passwordSet, ...database } = config.database
    const body = {
      revision: config.revision,
      database: { ...database, password: passwordEdited ? password : null },
      schedules: config.schedules,
      ...(config.runtime ? { runtime: config.runtime } : {}),
      integrations: {
        asharehub: { token: integrationDraft.asharehub_token ?? null },
        telegram: {
          token: integrationDraft.telegram_token ?? null,
          chat_id: integrationDraft.telegram_chat_id ?? null,
        },
      },
    }
    try {
      if (test) {
        const value = await systemConfigurationRequest<{ message: string }>('POST', body, true)
        setNotice(value.message)
      } else {
        setConfig(await systemConfigurationRequest<SystemConfiguration>('PUT', body))
        setPassword('')
        setPasswordEdited(false)
        setIntegrationDraft({})
        setNotice('系统配置已保存')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusy('')
    }
  }
  return (
    <div className="system-settings">
      <header>
        <span className="eyebrow">管理员专属</span>
        <h1>系统配置</h1>
      </header>
      {error && (
        <p role="alert" className="system-settings-error">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {config?.migration_warnings?.map((w) => (
        <p key={w} role="alert" className="system-settings-error">
          {w}
        </p>
      ))}
      <SystemNotificationConfiguration
        telegramConfigured={
          !!config?.integrations?.telegram.token_set && !!config?.integrations?.telegram.chat_id_set
        }
      />
      {!config ? (
        <button className="button" onClick={() => setReload((n) => n + 1)}>
          重新读取配置
        </button>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <fieldset disabled={!!busy}>
            <section className="panel system-settings-section">
              <h2>
                自动任务 <small>北京时间</small>
              </h2>
              <div className="system-task-list">
                {Object.entries(config.schedules).map(([key, t]) => (
                  <div className="system-task" key={key}>
                    <label className="system-task-toggle">
                      <input
                        type="checkbox"
                        checked={t.enabled}
                        onChange={(e) => updateTask(key, { enabled: e.target.checked })}
                      />
                      <span>{names[key] ?? key}</span>
                    </label>
                    <div className="system-task-inputs">
                      {t.telegram_notify !== undefined && (
                        <label className="system-task-notify">
                          <span>Telegram 通知</span>
                          <input
                            type="checkbox"
                            checked={t.telegram_notify}
                            onChange={(e) => updateTask(key, { telegram_notify: e.target.checked })}
                          />
                        </label>
                      )}
                      {t.frequency === 'month_end' && <small>每月最后一天</small>}
                      {t.times?.map((time, i) => (
                        <label key={i}>
                          <span>{t.times!.length > 1 ? `时间 ${i + 1}` : '每日时间'}</span>
                          <input
                            type="time"
                            required
                            value={time}
                            onChange={(e) =>
                              updateTask(key, {
                                times: t.times!.map((v, j) => (i === j ? e.target.value : v)),
                              })
                            }
                          />
                        </label>
                      ))}
                      {t.start !== undefined && (
                        <label>
                          开始
                          <input
                            type="time"
                            required
                            value={t.start}
                            onChange={(e) => updateTask(key, { start: e.target.value })}
                          />
                        </label>
                      )}
                      {t.end !== undefined && (
                        <label>
                          结束
                          <input
                            type="text"
                            inputMode="numeric"
                            required
                            pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00"
                            value={t.end}
                            onChange={(e) => updateTask(key, { end: e.target.value })}
                          />
                        </label>
                      )}
                      {t.interval_seconds !== undefined && (
                        <label>
                          间隔（秒）
                          <input
                            type="number"
                            required
                            min="1"
                            value={t.interval_seconds}
                            onChange={(e) =>
                              updateTask(key, { interval_seconds: Number(e.target.value) })
                            }
                          />
                        </label>
                      )}
                      {t.min_interval_minutes !== undefined && (
                        <label>
                          最短周期（分钟）
                          <input
                            type="number"
                            min="1"
                            max="1440"
                            required
                            value={t.min_interval_minutes}
                            onChange={(e) =>
                              updateTask(key, { min_interval_minutes: Number(e.target.value) })
                            }
                          />
                        </label>
                      )}
                      {t.max_interval_minutes !== undefined && (
                        <label>
                          最长周期（分钟）
                          <input
                            type="number"
                            min="1"
                            max="1440"
                            required
                            value={t.max_interval_minutes}
                            onChange={(e) =>
                              updateTask(key, { max_interval_minutes: Number(e.target.value) })
                            }
                          />
                        </label>
                      )}
                      {t.table !== undefined && (
                        <label>
                          持仓表名
                          <input
                            value={t.table}
                            onChange={(e) => updateTask(key, { table: e.target.value })}
                            autoComplete="off"
                          />
                        </label>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </section>
            {config.runtime && (
              <SystemRuntimeConfiguration
                value={config.runtime}
                statuses={config.data_sources ?? []}
                onChange={(runtime) => setConfig({ ...config, runtime })}
              />
            )}
            <section className="panel system-settings-section">
              <h2>数据接口与消息凭证</h2>
              <div className="system-database-fields">
                {[
                  {
                    key: 'asharehub_token',
                    label: 'AShareHub Token',
                    saved: config.integrations?.asharehub.token_set,
                  },
                  {
                    key: 'telegram_token',
                    label: 'Telegram 机器人密钥',
                    saved: config.integrations?.telegram.token_set,
                  },
                  {
                    key: 'telegram_chat_id',
                    label: 'Telegram 接收会话编号',
                    saved: config.integrations?.telegram.chat_id_set,
                  },
                ].map((item) => (
                  <label key={item.key}>
                    {item.label}
                    <input
                      aria-label={item.label}
                      type="password"
                      autoComplete="new-password"
                      value={integrationDraft[item.key] ?? ''}
                      placeholder={item.saved ? '已保存，未修改则保留' : '未配置'}
                      onChange={(e) =>
                        setIntegrationDraft((d) => ({ ...d, [item.key]: e.target.value }))
                      }
                    />
                    <button
                      type="button"
                      className="button button-quiet"
                      onClick={() => setIntegrationDraft((d) => ({ ...d, [item.key]: '' }))}
                    >
                      清除{item.label}
                    </button>
                    {integrationDraft[item.key] === '' && <small>保存后清除</small>}
                  </label>
                ))}
              </div>
              <p className="system-settings-muted">
                与数据库配置一起写入服务器配置文件；未修改的密钥保留，清除需点击保存。
              </p>
            </section>
            <section className="panel system-settings-section">
              <h2>统一数据库</h2>
              <div className="system-database-fields">
                {(
                  [
                    ['host', '主机'],
                    ['port', '端口'],
                    ['database', '数据库'],
                    ['username', '账号'],
                  ] as const
                ).map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <input
                      required
                      type={key === 'port' ? 'number' : 'text'}
                      min={key === 'port' ? 1 : undefined}
                      max={key === 'port' ? 65535 : undefined}
                      autoComplete="off"
                      value={config.database[key]}
                      onChange={(e) =>
                        setConfig({
                          ...config,
                          database: {
                            ...config.database,
                            [key]: key === 'port' ? Number(e.target.value) : e.target.value,
                          },
                        })
                      }
                    />
                  </label>
                ))}
                <label>
                  密码
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    placeholder={config.database.password_set ? '留空保留已保存密码' : ''}
                    onChange={(e) => {
                      setPassword(e.target.value)
                      setPasswordEdited(true)
                    }}
                  />
                </label>
              </div>
              <p className="system-settings-muted">
                修改数据库目标或账号时，请重新输入密码。不会搬迁或删除旧库数据。
              </p>
              <button
                type="button"
                className="button button-quiet"
                onClick={() => void submit(true)}
              >
                {busy === 'test' ? '测试中…' : '测试数据库连接'}
              </button>
            </section>
            <div className="system-settings-actions">
              <button
                type="button"
                className="button button-quiet"
                onClick={() => {
                  setPassword('')
                  setPasswordEdited(false)
                  setIntegrationDraft({})
                  setNotice('')
                  setError('')
                  setConfig(null)
                  setReload((n) => n + 1)
                }}
              >
                重新读取配置
              </button>
              <button className="button button-primary" type="submit">
                {busy === 'save' ? '保存中…' : '保存系统配置'}
              </button>
            </div>
          </fieldset>
        </form>
      )}
    </div>
  )
}
