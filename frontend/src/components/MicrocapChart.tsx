import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { MicrocapPoint } from '../types/microcap'

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  AriaComponent,
  CanvasRenderer,
])

export const temperatureViews = [
  { key: 'score', label: '综合温度' },
  { key: 'cap_pct', label: '市值分位' },
  { key: 'relative_pct', label: '相对估值' },
  { key: 'crowding_pct', label: '换手拥挤度' },
  { key: 'amount_crowding_pct', label: '成交额拥挤度' },
] as const

export type TemperatureView = (typeof temperatureViews)[number]['key']
type ChartView = TemperatureView | 'valuation' | 'profit'
type Metric = Exclude<keyof MicrocapPoint, 'date'>

export function MicrocapChart({
  points,
  view,
  overlayIndex = true,
}: {
  points: MicrocapPoint[]
  view: ChartView
  overlayIndex?: boolean
}) {
  const element = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)
  const title =
    view === 'valuation'
      ? 'PE / PB 中位数'
      : view === 'profit'
        ? '归母净利润增速中位数'
        : temperatureViews.find((item) => item.key === view)!.label
  useEffect(() => {
    if (!element.current) return
    chart.current = init(element.current)
    const observer = new ResizeObserver(() => chart.current?.resize())
    observer.observe(element.current)
    return () => {
      observer.disconnect()
      chart.current?.dispose()
      chart.current = null
    }
  }, [])
  useEffect(() => {
    const isTemperature = view !== 'valuation' && view !== 'profit'
    const secondAxis = view === 'valuation' || (isTemperature && overlayIndex)
    const line = (
      field: Metric,
      name: string,
      multiplier: number,
      unit: string,
      axis: number,
      color: string,
    ) => ({
      type: 'line',
      name,
      yAxisIndex: axis,
      data: points.map((point) => (point[field] == null ? null : point[field]! * multiplier)),
      connectNulls: false,
      smooth: false,
      showSymbol: points.length < 3,
      lineStyle: { width: 2.3, color },
      itemStyle: { color },
      tooltip: {
        valueFormatter: (value: unknown) =>
          value == null ? '暂无数据' : Number(value).toFixed(2) + unit,
      },
    })
    const primary = isTemperature
      ? {
          ...line(view, title, 100, view === 'score' ? ' 分' : '%', 0, '#efc482'),
          areaStyle: { opacity: 0.07 },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { position: 'insideEndTop', color: '#87978a', formatter: '{c}' },
            lineStyle: { color: '#71816b', type: 'dashed', opacity: 0.5 },
            data: [{ yAxis: 20 }, { yAxis: 80 }],
          },
        }
      : view === 'valuation'
        ? line('pe_median', 'PE 中位数', 1, ' 倍', 0, '#efc482')
        : line('avg_np_growth', '归母净利润同比增速', 1, '%', 0, '#8edbd6')
    const series = [primary]
    if (view === 'valuation') series.push(line('pb_median', 'PB 中位数', 1, ' 倍', 1, '#8edbd6'))
    else if (isTemperature && overlayIndex)
      series.push(line('micro_index', '微盘 1000 等权指数', 1, ' 点', 1, '#8edbd6'))
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 650,
        aria: { enabled: true, description: `微盘${title}历史曲线，按五交易日采样展示。` },
        legend: { top: 4, textStyle: { color: '#b1bcae', fontSize: 11 }, type: 'scroll' },
        grid: { left: 8, right: 8, top: 58, bottom: 78, containLabel: true },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: '#24342c',
          borderColor: '#46584b',
          textStyle: { color: '#f0f5ec', fontSize: 12 },
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((point) => point.date),
          axisLabel: { color: '#88978e', hideOverlap: true, fontSize: 10 },
          axisTick: { show: false },
          axisLine: { lineStyle: { color: '#344038' } },
        },
        yAxis: [
          {
            type: 'value',
            name: isTemperature
              ? view === 'score'
                ? '温度'
                : '分位 %'
              : view === 'valuation'
                ? 'PE 倍'
                : '同比 %',
            nameTextStyle: { color: '#afbaaa', fontSize: 10 },
            ...(isTemperature ? { min: 0, max: 100 } : { scale: true }),
            axisLabel: { color: '#a6afa0', fontSize: 10 },
            splitLine: { lineStyle: { color: '#2b3730', type: 'dashed' } },
          },
          {
            type: 'value',
            show: secondAxis,
            name: view === 'valuation' ? 'PB 倍' : '指数点位',
            scale: true,
            nameTextStyle: { color: '#8edbd6', fontSize: 10 },
            axisLabel: { color: '#8caeab', fontSize: 10 },
            splitLine: { show: false },
          },
        ],
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: false },
          {
            type: 'slider',
            bottom: 10,
            height: 22,
            borderColor: 'transparent',
            fillerColor: 'rgba(239,196,130,0.12)',
            handleStyle: { color: '#efc482' },
            textStyle: { color: '#88978e', fontSize: 10 },
          },
        ],
        series,
      },
      { notMerge: true },
    )
  }, [points, view, overlayIndex, title])
  return (
    <div ref={element} className="trend-chart" role="img" aria-label={`微盘${title}历史曲线`} />
  )
}
