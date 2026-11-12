import { SystemTaskConfiguration } from '../components/SystemTaskConfiguration'
import { SystemRuntimeConfiguration } from '../components/SystemRuntimeConfiguration'
import { useEffect, useState } from 'react'
import { useAuth } from '../components/AccountAccess'
import { SystemNotificationConfiguration } from '../components/SystemNotificationConfiguration'
import { systemConfigurationRequest } from '../api/systemConfiguration'
import type { SystemConfiguration, TaskConfiguration } from '../api/systemConfiguration'
import '../styles/system-configuration.css'
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
        <nav className="system-settings-nav" aria-label="配置分区">
          <a href="#settings-tasks">自动任务</a>
          <a href="#settings-runtime">运行参数</a>
          <a href="#settings-credentials">接口凭证</a>
          <a href="#settings-database">数据库</a>
          <a href="#settings-notifications">社交通知</a>
        </nav>
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
            <SystemTaskConfiguration schedules={config.schedules} onChange={updateTask} />
            {config.runtime && (
              <SystemRuntimeConfiguration
                value={config.runtime}
                statuses={config.data_sources ?? []}
                onChange={(runtime) => setConfig({ ...config, runtime })}
              />
            )}
            <section
              id="settings-credentials"
              className="panel system-settings-section system-credentials"
            >
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
            <section id="settings-database" className="panel system-settings-section">
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
      <SystemNotificationConfiguration
        telegramConfigured={
          !!config?.integrations?.telegram.token_set && !!config?.integrations?.telegram.chat_id_set
        }
      />
    </div>
  )
}
