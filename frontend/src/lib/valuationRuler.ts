import type { ValuationHistory, ValuationPoint } from '../types/valuationRuler'
import { chartColors } from './chartTheme'
export const valuationNumber = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value)
    ? '—'
    : value.toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })
export function valuationSource(source: string | null | undefined) {
  if (!source) return '来源未记录'
  if (source === 'joinquant' || source === 'JQ_PLATFORM.PE_TTM.V1') return '聚宽历史'
  if (source === 'csi_em' || source === 'CSI_EM.PE_TTM.V1') return '中证成分 / 东方财富'
  return source
}
export function valuationChartOption(data: ValuationHistory, days: number) {
  const points = data.points
  const last = points.at(-1)?.date
  const cutoff =
    last && days
      ? new Date(Date.parse(last + 'T00:00:00Z') - days * 86400000).toISOString().slice(0, 10)
      : null
  const segments: { start: number; end: number }[] = []
  points.forEach((point, index) => {
    if (point.gap || point.mean_pe_ttm == null) return
    const prev = points[index - 1]
    if (!prev || prev.gap || prev.mean_pe_ttm == null || prev.source !== point.source)
      segments.push({ start: index, end: index })
    else segments[segments.length - 1].end = index
  })
  const line = (name: string, color: string, values: (number | null)[], yAxisIndex: number) => ({
    name,
    type: 'line',
    yAxisIndex,
    data: values,
    smooth: false,
    connectNulls: false,
    showSymbol: false,
    itemStyle: { color },
    lineStyle: { width: 2 },
  })
  return {
    animation: false,
    aria: { enabled: true },
    grid: { left: 20, right: 24, top: 90, bottom: 80, containLabel: true },
    legend: { top: 8, left: 12, right: 12, textStyle: { color: chartColors.muted } },
    tooltip: {
      trigger: 'axis',
      renderMode: 'richText',
      confine: true,
      formatter: (params: unknown) => {
        const items = (Array.isArray(params) ? params : [params]) as { dataIndex?: number }[]
        const p: ValuationPoint | undefined = points[items[0]?.dataIndex ?? -1]
        return p
          ? [
              p.date,
              valuationSource(p.source),
              `沪深300：${valuationNumber(p.index_close)}`,
              `平均PE-TTM：${valuationNumber(p.mean_pe_ttm)}`,
              p.gap
                ? '缺失交易日，未填充'
                : `有效正PE ${p.positive_count ?? '—'} / 负PE ${p.loss_count ?? '—'} / 缺失PE ${p.missing_pe_count ?? 0} / 成分 ${p.member_count ?? '—'}`,
            ].join('\n')
          : ''
      },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: points.map((p) => p.date),
      axisLabel: { hideOverlap: true },
    },
    yAxis: [
      {
        type: 'value',
        name: '沪深300指数',
        scale: true,
        splitLine: { lineStyle: { color: chartColors.grid } },
      },
      { type: 'value', name: '平均PE-TTM（倍）', scale: true, splitLine: { show: false } },
    ],
    dataZoom: [
      {
        type: 'inside',
        zoomOnMouseWheel: 'ctrl',
        startValue: cutoff ? points.find((p) => p.date >= cutoff)?.date : points[0]?.date,
        endValue: last,
      },
      { type: 'slider', bottom: 12, height: 24 },
    ],
    series: [
      ...segments.map((segment, index) => ({
        ...line(
          '沪深300指数',
          '#3568d4',
          points.map((p, i) => (i >= segment.start && i <= segment.end ? p.index_close : null)),
          0,
        ),
        id: `index-${index}`,
        showSymbol: segment.start === segment.end,
      })),
      ...segments.map((segment, index) => ({
        ...line(
          '成分股平均PE-TTM',
          chartColors.gold,
          points.map((p, i) => (i >= segment.start && i <= segment.end ? p.mean_pe_ttm : null)),
          1,
        ),
        id: `pe-${index}`,
        showSymbol: segment.start === segment.end,
      })),
      ...(
        [
          { key: 'p20', name: '历史P20', color: chartColors.jade },
          { key: 'p80', name: '历史P80', color: chartColors.coral },
        ] as const
      ).map((item) => ({
        ...line(
          item.name,
          item.color,
          points.map(() => data.benchmarks[item.key]),
          1,
        ),
        lineStyle: { type: 'dashed', width: 1.5 },
      })),
    ],
  }
}
