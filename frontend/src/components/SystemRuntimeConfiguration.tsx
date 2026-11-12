export type RuntimeConfiguration = {
  logging: { level: string; retention_days: number }
  database_sync: { read_timeout_seconds: number; attempts: number }
  sources: Record<string, { request_timeout_seconds: number }>
}
export type DataSourceStatus = {
  id: string
  label?: string
  hosts?: string[]
  request_timeout_seconds?: number
  credential_required?: boolean
  credential_set?: boolean
  status?: 'configured' | 'credential_missing'
  health?: 'not_checked'
  read_only?: boolean
}
const sourceNames: Record<string, string> = {
  eastmoney: '东方财富',
  asharehub: 'AShareHub',
  tdx: '通达信',
  sse: '上交所',
  szse: '深交所',
  tencent: '腾讯',
  sina: '新浪',
  ccmn: '长江有色',
  coalindex: 'CCI 网络报价',
  csi: '中证指数',
}
export function SystemRuntimeConfiguration({
  value,
  onChange,
  statuses = [],
}: {
  value: RuntimeConfiguration
  onChange: (value: RuntimeConfiguration) => void
  statuses: DataSourceStatus[]
}) {
  return (
    <section className="panel system-settings-section">
      <h2>运行参数</h2>
      <div className="system-runtime-groups">
        <section>
          <h3>日志</h3>
          <div className="system-database-fields">
            <label>
              日志级别
              <select
                value={value.logging.level}
                onChange={(e) =>
                  onChange({ ...value, logging: { ...value.logging, level: e.target.value } })
                }
              >
                {['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((level) => (
                  <option key={level}>{level}</option>
                ))}
              </select>
            </label>
            <label>
              保留天数
              <input
                type="number"
                min="1"
                max="3650"
                required
                value={value.logging.retention_days}
                onChange={(e) =>
                  onChange({
                    ...value,
                    logging: { ...value.logging, retention_days: Number(e.target.value) },
                  })
                }
              />
            </label>
          </div>
        </section>
        <section>
          <h3>数据库同步</h3>
          <div className="system-database-fields">
            <label>
              读取超时（秒）
              <input
                type="number"
                min="5"
                max="3600"
                required
                value={value.database_sync.read_timeout_seconds}
                onChange={(e) =>
                  onChange({
                    ...value,
                    database_sync: {
                      ...value.database_sync,
                      read_timeout_seconds: Number(e.target.value),
                    },
                  })
                }
              />
            </label>
            <label>
              尝试次数（含首次）
              <input
                type="number"
                min="1"
                max="5"
                required
                value={value.database_sync.attempts}
                onChange={(e) =>
                  onChange({
                    ...value,
                    database_sync: { ...value.database_sync, attempts: Number(e.target.value) },
                  })
                }
              />
            </label>
          </div>
        </section>
      </div>
      <h3>数据源请求超时</h3>
      <div className="system-source-grid">
        {Object.entries(value.sources).map(([id, source]) => {
          const status = statuses.find((item) => item.id === id)
          return (
            <div className="system-source-card" key={id}>
              <label>
                {sourceNames[id] ?? id}
                <input
                  aria-label={`${sourceNames[id] ?? id}请求超时（秒）`}
                  type="number"
                  min="5"
                  max="600"
                  required
                  value={source.request_timeout_seconds}
                  onChange={(e) =>
                    onChange({
                      ...value,
                      sources: {
                        ...value.sources,
                        [id]: { request_timeout_seconds: Number(e.target.value) },
                      },
                    })
                  }
                />
              </label>
              <small>
                {status?.credential_required && !status.credential_set
                  ? '未配置'
                  : status?.credential_required && status.credential_set
                    ? '已配置'
                    : '公开数据源'}{' '}
                · 尚未检测可用性
              </small>
              {status?.hosts?.length ? (
                <small title={status.hosts.join(', ')}>{status.hosts.join(', ')}</small>
              ) : null}
            </div>
          )
        })}
      </div>
    </section>
  )
}
