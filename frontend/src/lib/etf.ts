import type { ETFCollectionStatus, ETFHistory } from '../types/etf'

export const etfColors = ['#c44837', '#337fa5', '#ad7c14', '#217b70', '#8d63a6']
export const billionShares = (value: string | null) =>
  value == null ? '—' : (Number(value) / 1e8).toFixed(2)
export const signedPercent = (value: number | null) =>
  value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`

export function chartSeries(data: ETFHistory, relative: boolean, selected: string | null) {
  const dates = [
    ...new Set(data.funds.flatMap((fund) => fund.points.map((point) => point.date))),
  ].sort()
  const series = data.funds.flatMap((fund, index) => {
    if (selected && fund.code !== selected) return []
    const points = new Map(fund.points.map((point) => [point.date, point]))
    return [
      {
        name: `${fund.name} ${fund.code.split('.')[0]}`,
        color: etfColors[index % etfColors.length],
        values: dates.map((date) => {
          const point = points.get(date)
          return point ? (relative ? point.change_pct : Number(point.shares) / 1e8) : null
        }),
      },
    ]
  })
  return { dates, series }
}

export const etfGroups = [
  { id: 'hs300', name: '沪深300' },
  { id: 'zz500', name: '中证500' },
  { id: 'cyb', name: '创业板' },
  { id: 'kc50', name: '科创50' },
  { id: 'zz1000', name: '中证1000' },
  { id: 'sz50', name: '上证50' },
  { id: 'sz180', name: '上证180' },
]

export function marketValueSeries(data: ETFHistory) {
  const points = new Map((data.market_values ?? []).map((point) => [point.date, point]))
  const dates = [
    ...new Set([
      ...points.keys(),
      ...data.funds.flatMap((fund) => fund.points.map((point) => point.date)),
    ]),
  ].sort()
  return {
    dates,
    series: [
      {
        name: `${data.group_name} 合计流通市值`,
        color: etfColors[0],
        values: dates.map((date) => {
          const point = points.get(date)
          if (
            !point ||
            point.market_value == null ||
            (point.quality_flags?.length ?? 0) > 0 ||
            (point.excluded_funds?.length ?? 0) > 0 ||
            point.market_value.trim() === '' ||
            point.expected_funds <= 0 ||
            point.available_funds !== point.expected_funds
          )
            return null
          const value = Number(point.market_value)
          return Number.isFinite(value) && value >= 0 ? value / 1e8 : null
        }),
      },
    ],
  }
}

export const showETFSymbols = (values: Array<number | null>) =>
  values.filter((value) => value != null).length < 100

export function collectionSummary(status: ETFCollectionStatus) {
  if (status.status === 'disabled') return '自动采集已关闭，当前展示已保存的数据。'
  if (status.status === 'missing_writer_configuration')
    return '自动采集未就绪：尚未配置独立写入账号。页面同步只读取已有数据。'
  if (
    status.status === 'writer_target_mismatch' ||
    status.status === 'invalid_writer_configuration'
  )
    return '自动采集未就绪：写入连接配置不正确，请检查是否指向同一数据库。'
  if (
    status.status === 'writer_permission_denied' ||
    status.runs?.[0]?.error === 'writer_permission_denied'
  )
    return '自动采集失败：数据库写入权限不足，请检查采集账号。'
  if (status.status.startsWith('writer_'))
    return '自动采集写入失败，请检查采集账号、目标表和数据库连接；已保存数据保留。'
  if (status.status !== 'configured' || !status.worker_running)
    return '自动采集尚未运行，请检查服务状态；当前展示已保存的数据。'
  const latest = status.runs?.[0]
  const prefix =
    latest && ['failed', 'partial'].includes(latest.status)
      ? '最近一次采集未完整完成，保留已保存数据。'
      : ''
  return `${prefix}自动采集：北京时间 ${(status.schedule ?? []).join('、')}；历史目标 ${status.target_sessions ?? 300} 个交易日，按批次补采。`
}
