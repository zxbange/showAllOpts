import type { ETFCollectionStatus, ETFHistory, ETFManualRun } from '../types/etf'

export const etfColors = ['#c44837', '#337fa5', '#ad7c14', '#217b70', '#8d63a6']
export const billionShares = (value: string | null) =>
  value == null ? '—' : (Number(value) / 1e8).toFixed(2)

export function recentShareRows(data: ETFHistory) {
  // All funds share one trading-date axis; missing observations stay empty.
  const availableDates = [
    ...new Set([
      ...(data.market_values ?? []).map((point) => point.date),
      ...data.funds.flatMap((fund) => fund.points.map((point) => point.date)),
      ...(data.end_date ? [data.end_date] : []),
    ]),
  ]
    .filter((date) => !data.end_date || date <= data.end_date)
    .sort()
    .slice(-5)
  const dates: Array<string | null> = [
    ...Array<null>(5 - availableDates.length).fill(null),
    ...availableDates,
  ]
  const rows = data.funds.map((fund) => {
    const points = new Map(fund.points.map((point) => [point.date, point.shares]))
    const shares = dates.map((date) => (date ? (points.get(date) ?? null) : null))
    const [previous, current] = shares.slice(-2)
    const change =
      previous == null || current == null ? null : (BigInt(current) - BigInt(previous)).toString()
    return { fund, shares, change }
  })
  return { dates, rows }
}

export function chartSeries(data: ETFHistory, relative: boolean, selected: string | null) {
  const dates = [
    ...new Set([
      ...(data.market_values ?? []).map((point) => point.date),
      ...data.funds.flatMap((fund) => fund.points.map((point) => point.date)),
    ]),
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

// A terminal mutation response must not retain progress from its previous running snapshot.
export function withManualRun(
  status: ETFCollectionStatus,
  manual: ETFManualRun,
): ETFCollectionStatus {
  if (['queued', 'running', 'cancelling'].includes(manual.status)) return { ...status, manual }
  return {
    ...status,
    manual,
    progress: null,
    status:
      status.status === 'running'
        ? status.enabled === false
          ? 'disabled'
          : 'configured'
        : status.status,
  }
}

export function collectionErrorMessage(error: string): string {
  const messages: Record<string, string> = {
    writer_permission_denied: '采集账号权限不足，请检查三张ETF表的读写权限。',
    writer_table_missing: 'ETF数据表缺失，首次建表需要CREATE权限。',
    writer_database_missing: '目标数据库不存在，请检查数据库配置。',
    writer_connection_failed: '无法连接数据库，请检查网络、账号和数据库配置。',
    writer_write_failed: '数据库写入未完成，请检查采集账号及目标表。',
    writer_target_mismatch: '写入目标与页面数据库不一致，请重新读取配置并保存。',
    invalid_writer_configuration: '采集配置无效，请重新检查并保存。',
    configuration_unavailable: '采集配置暂不可用，请重新读取配置。',
    source_unavailable: '交易所数据暂不可用，请稍后重试。',
    service_interrupted: '服务重启或采集被中断，可重新触发采集。',
    configuration_changed: '数据库目标已变更，本次任务已中断；请重新配置后采集。',
    missing_writer_configuration: '请先配置采集数据库连接。',
    retry_exhausted: '当前目标日已达到自动重试上限，可勾选重新尝试；仅更新日常目标，不回补历史。',
  }
  return (
    messages[error] ??
    (/[\u4e00-\u9fff]/.test(error) ? error : '操作未完成，请稍后重试或检查采集配置。')
  )
}
