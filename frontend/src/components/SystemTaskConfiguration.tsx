import type { TaskConfiguration } from '../api/systemConfiguration'

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

export function SettingsSwitch({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <label className="system-switch">
      <input
        type="checkbox"
        role="switch"
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="system-switch-track" aria-hidden="true">
        <span />
      </span>
      <span className="system-switch-state" aria-hidden="true">
        {checked ? '开启' : '关闭'}
      </span>
    </label>
  )
}

function TimeInput({
  label,
  value,
  onChange,
  end = false,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  end?: boolean
}) {
  return (
    <input
      className="system-time-input"
      type="text"
      inputMode="numeric"
      aria-label={label}
      required
      maxLength={5}
      pattern={end ? '(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00' : '(?:[01][0-9]|2[0-3]):[0-5][0-9]'}
      placeholder="HH:mm"
      title={end ? '24 小时制 HH:mm；结束时间可用 24:00' : '24 小时制 HH:mm，例如 18:30'}
      autoComplete="off"
      value={value}
      onChange={(event) => {
        const text = event.target.value
        onChange(/^\d{3,4}$/.test(text) ? `${text.slice(0, 2)}:${text.slice(2)}` : text)
      }}
    />
  )
}

export function SystemTaskConfiguration({
  schedules,
  onChange,
}: {
  schedules: Record<string, TaskConfiguration>
  onChange: (key: string, patch: Partial<TaskConfiguration>) => void
}) {
  return (
    <section id="settings-tasks" className="panel system-settings-section system-schedules">
      <div className="system-section-heading">
        <h2>自动任务</h2>
        <span className="system-timezone">北京时间 · 24h</span>
      </div>
      <p className="system-settings-muted system-task-help">
        自动运行控制定时执行，通知开关只控制推送；修改后保存生效。
      </p>
      <div className="system-task-table" role="table" aria-label="自动任务配置">
        <div className="system-task-columns" role="row">
          {['任务', '自动运行', '频率', '运行时间', 'Telegram 通知'].map((title) => (
            <span role="columnheader" key={title}>
              {title}
            </span>
          ))}
        </div>
        {Object.entries(schedules).map(([key, t]) => (
          <div className="system-task" role="row" aria-label={names[key] ?? key} key={key}>
            <div className="system-task-name" role="cell">
              <span>{names[key] ?? key}</span>
              {t.table !== undefined && (
                <label className="system-table-name">
                  持仓表名
                  <input
                    value={t.table}
                    onChange={(e) => onChange(key, { table: e.target.value })}
                    autoComplete="off"
                  />
                </label>
              )}
            </div>
            <div className="system-task-enabled" role="cell" data-label="自动运行">
              <SettingsSwitch
                label="自动运行"
                checked={t.enabled}
                onChange={(enabled) => onChange(key, { enabled })}
              />
            </div>
            <div className="system-task-frequency" role="cell" data-label="频率">
              <span className="system-frequency-badge">
                {t.frequency === 'month_end'
                  ? '每月最后一天'
                  : key === 'social_cubes'
                    ? '周一至周五'
                    : t.interval_seconds !== undefined || t.min_interval_minutes !== undefined
                      ? '每天轮询'
                      : '每天'}
              </span>
              {t.interval_seconds !== undefined && (
                <label className="system-frequency-input">
                  <span>每</span>
                  <input
                    aria-label="间隔（秒）"
                    type="number"
                    required
                    min="1"
                    value={t.interval_seconds}
                    onChange={(e) => onChange(key, { interval_seconds: Number(e.target.value) })}
                  />
                  <span>秒</span>
                </label>
              )}
              {t.min_interval_minutes !== undefined && (
                <div className="system-frequency-input">
                  <input
                    aria-label="最短周期（分钟）"
                    type="number"
                    required
                    min="1"
                    max="1440"
                    value={t.min_interval_minutes}
                    onChange={(e) =>
                      onChange(key, { min_interval_minutes: Number(e.target.value) })
                    }
                  />
                  <span>–</span>
                  <input
                    aria-label="最长周期（分钟）"
                    type="number"
                    required
                    min="1"
                    max="1440"
                    value={t.max_interval_minutes}
                    onChange={(e) =>
                      onChange(key, { max_interval_minutes: Number(e.target.value) })
                    }
                  />
                  <span>分钟</span>
                </div>
              )}
            </div>
            <div className="system-task-times" role="cell" data-label="运行时间">
              {t.times?.map((time, i) => (
                <TimeInput
                  key={i}
                  label={t.times!.length > 1 ? `时间 ${i + 1}` : '每日时间'}
                  value={time}
                  onChange={(value) =>
                    onChange(key, { times: t.times!.map((v, j) => (i === j ? value : v)) })
                  }
                />
              ))}
              {t.start !== undefined && (
                <TimeInput
                  label="开始"
                  value={t.start}
                  onChange={(start) => onChange(key, { start })}
                />
              )}
              {t.start !== undefined && t.end !== undefined && (
                <span className="system-time-divider">–</span>
              )}
              {t.end !== undefined && (
                <TimeInput
                  label="结束"
                  value={t.end}
                  end
                  onChange={(end) => onChange(key, { end })}
                />
              )}
            </div>
            <div className="system-task-notify" role="cell" data-label="Telegram 通知">
              {t.telegram_notify !== undefined ? (
                <SettingsSwitch
                  label="Telegram 通知"
                  checked={t.telegram_notify}
                  onChange={(telegram_notify) => onChange(key, { telegram_notify })}
                />
              ) : key === 'social' || key === 'social_cubes' ? (
                <a className="system-not-applicable" href="#settings-notifications">
                  单独配置
                </a>
              ) : (
                <span className="system-not-applicable">不适用</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
