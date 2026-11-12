import { SettingsSwitch } from './SystemTaskConfiguration'
import { useEffect, useState } from 'react'
import { Send } from 'lucide-react'
import { socialRequest as api } from '../api/social'
import '../styles/social.css'
type NotificationChannel = 'telegram' | 'feishu'
type NotificationSetting = 'feishu_webhook' | 'feishu_secret'
const notificationChannels = [
  {
    id: 'telegram',
    title: 'Telegram',
    fields: [],
  },
  {
    id: 'feishu',
    title: '飞书',
    fields: [
      ['feishu_webhook', '飞书 Webhook', 500],
      ['feishu_secret', '飞书签名密钥', 300],
    ],
  },
] as const
type Status = {
  telegram_enabled?: boolean
  feishu_enabled?: boolean
  token_configured?: boolean
  chat_id_configured?: boolean
  feishu_webhook_configured?: boolean
  feishu_secret_configured?: boolean
}
export function SystemNotificationConfiguration({
  telegramConfigured = false,
}: {
  telegramConfigured?: boolean
}) {
  const [status, setStatus] = useState<Status | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [secrets, setSecrets] = useState({
    feishu_webhook: '',
    feishu_secret: '',
  })
  const [editingSecrets, setEditingSecrets] = useState({
    feishu_webhook: false,
    feishu_secret: false,
  })
  useEffect(() => {
    const c = new AbortController()
    api<Status>('/status', undefined, c.signal)
      .then(setStatus)
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message)
      })
    return () => c.abort()
  }, [])
  async function action(work: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await work()
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }
  function saveNotificationConfig(channel: NotificationChannel, key?: NotificationSetting) {
    if (!status) return
    const fields = notificationChannels.find((item) => item.id === channel)!.fields
    const changes = key
      ? { [key]: secrets[key].trim() }
      : Object.fromEntries(
          fields
            .filter(([field]) => !status[`${field}_configured`] && secrets[field].trim())
            .map(([field]) => [field, secrets[field].trim()]),
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

  function notificationEnabled(channel: NotificationChannel) {
    return channel === 'telegram' ? status?.telegram_enabled !== false : !!status?.feishu_enabled
  }

  function toggleNotification(channel: NotificationChannel) {
    if (!status) return
    const enabled = !notificationEnabled(channel)
    const item = notificationChannels.find((entry) => entry.id === channel)!
    if (
      enabled &&
      (channel === 'telegram'
        ? !telegramConfigured
        : item.fields.some(([field]) => !status[`${field}_configured`]))
    ) {
      setNotice('')
      setError(
        channel === 'telegram'
          ? '请先在“数据接口与消息凭证”保存 Telegram 机器人密钥和接收会话编号。'
          : `请先保存${item.title}的${item.fields.map(([, title]) => title).join('和')}。`,
      )
      return
    }
    void action(async () => {
      setStatus(await api<Status>('/config', { [`${channel}_enabled`]: enabled }))
      setNotice(`${item.title}通知已${enabled ? '开启' : '关闭'}，采集开关保持不变。`)
    })
  }

  if (!status)
    return (
      <section id="settings-notifications" className="panel system-settings-section">
        <h2>社交监控通知</h2>
        <p role="status">{error || '正在读取通知配置…'}</p>
      </section>
    )
  return (
    <section id="settings-notifications" className="panel system-settings-section">
      <h2>社交监控通知</h2>
      <p className="system-settings-muted">
        此区域独立保存，开关即时生效；Telegram 凭证与采集任务共用。
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <h3>通知渠道</h3>
      <p className="social-muted">
        此处开关仅控制社交监控通知；采集任务使用各自的 Telegram 开关。测试消息需开启对应社交通知。
      </p>
      {!notificationEnabled('telegram') && !notificationEnabled('feishu') && (
        <p className="social-notice">通知渠道均已关闭，监控运行时只采集、不推送。</p>
      )}
      {notificationChannels.map((channel) => {
        const enabled = notificationEnabled(channel.id)
        const configured =
          channel.id === 'telegram'
            ? telegramConfigured
            : channel.fields.every(([field]) => status[`${field}_configured`])
        return (
          <section
            className="social-notification-channel"
            key={channel.id}
            aria-labelledby={`social-channel-${channel.id}`}
          >
            <div className="social-panel-heading">
              <h3 id={`social-channel-${channel.id}`}>{channel.title} 通知配置</h3>
              <SettingsSwitch
                label={`启用${channel.title}通知`}
                checked={enabled}
                disabled={busy}
                onChange={() => toggleNotification(channel.id)}
              />
            </div>
            {channel.id === 'feishu' && (
              <p className="social-muted">
                在目标飞书群添加自定义机器人，开启签名校验后填写以下两项。
                <a
                  href="https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot?lang=zh-CN"
                  target="_blank"
                  rel="noreferrer"
                >
                  配置方法
                </a>
              </p>
            )}
            <div className="social-form">
              <div className="social-field-grid">
                {channel.fields.map(([key, title, maxLength]) => {
                  const saved = !!status[`${key}_configured`]
                  return (
                    <form
                      className="social-credential-field"
                      key={key}
                      onSubmit={(event) => {
                        event.preventDefault()
                        saveNotificationConfig(channel.id, saved ? key : undefined)
                      }}
                    >
                      <div className="social-label-row">
                        <span>{title}</span>
                        <span className={`social-config-state ${saved ? 'configured' : ''}`}>
                          {saved ? '已配置' : '未配置'}
                        </span>
                      </div>
                      {saved && !editingSecrets[key] ? (
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
                          onChange={(event) =>
                            setSecrets((current) => ({ ...current, [key]: event.target.value }))
                          }
                          placeholder={saved ? `填写新的${title}` : `填写 ${title}`}
                          maxLength={maxLength}
                        />
                      )}
                      {saved && editingSecrets[key] && (
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
                  )
                })}
              </div>
              <div className="social-actions">
                {channel.id !== 'telegram' && !configured && (
                  <button
                    type="button"
                    disabled={
                      busy ||
                      !channel.fields.some(
                        ([field]) => !status[`${field}_configured`] && secrets[field].trim(),
                      )
                    }
                    className="button button-primary"
                    onClick={() => saveNotificationConfig(channel.id)}
                  >
                    保存飞书配置
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || !configured || !enabled}
                  className="button"
                  onClick={() =>
                    void action(async () => {
                      const result = await api<{ message: string }>(`/${channel.id}-test`, {})
                      setNotice(result.message)
                    })
                  }
                >
                  <Send size={15} />
                  {channel.id === 'telegram' ? '发送测试消息' : '发送飞书测试消息'}
                </button>
              </div>
            </div>
          </section>
        )
      })}
    </section>
  )
}
