import { chartColors } from '../lib/chartTheme'
import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkPointComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsType } from 'echarts/core'
import type { HistoryPoint } from '../types/momentum'

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkPointComponent,
  AriaComponent,
  CanvasRenderer,
])

export function TrendChart({
  points,
  name,
  allowNegative = false,
  precision = 2,
  seriesLabel = '动量热力值',
}: {
  points: HistoryPoint[]
  name: string
  allowNegative?: boolean
  precision?: number
  seriesLabel?: string
}) {
  const element = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)
  useEffect(() => {
    if (!element.current) return
    chart.current = init(element.current, undefined, { renderer: 'canvas' })
    const resize = new ResizeObserver(() => chart.current?.resize())
    resize.observe(element.current)
    return () => {
      resize.disconnect()
      chart.current?.dispose()
      chart.current = null
    }
  }, [])
  useEffect(() => {
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 700,
        aria: {
          enabled: true,
          description: `${name}的历史动量曲线。缺失记录以断点展示，不填充为零。`,
        },
        grid: { left: 14, right: 24, top: 28, bottom: 76, containLabel: true },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: chartColors.background,
          borderColor: chartColors.border,
          textStyle: { color: chartColors.text, fontSize: 12 },
          valueFormatter: (value: unknown) =>
            value == null || value === '-' ? '暂无记录' : Number(value).toFixed(precision),
          axisPointer: { type: 'line', lineStyle: { color: chartColors.coral, type: 'dashed' } },
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((p) => p.date),
          axisLine: { lineStyle: { color: chartColors.border } },
          axisTick: { show: false },
          axisLabel: {
            color: chartColors.muted,
            fontSize: 12,
            hideOverlap: true,
            formatter: (value: string) => value.slice(5).replace('-', '/'),
          },
        },
        yAxis: {
          type: 'value',
          min: allowNegative ? undefined : 0,
          splitNumber: 4,
          axisLabel: { color: chartColors.muted, fontSize: 12 },
          splitLine: { lineStyle: { color: chartColors.grid, type: 'dashed' } },
        },
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: false, moveOnMouseWheel: false },
          {
            type: 'slider',
            height: 22,
            bottom: 12,
            borderColor: 'transparent',
            backgroundColor: chartColors.surface,
            fillerColor: chartColors.coralFill,
            dataBackground: {
              lineStyle: { color: chartColors.coral },
              areaStyle: { color: chartColors.surface },
            },
            selectedDataBackground: {
              lineStyle: { color: chartColors.coral },
              areaStyle: { color: chartColors.coralFill },
            },
            handleStyle: { color: chartColors.coral, borderColor: chartColors.coral },
            textStyle: { color: chartColors.muted },
          },
        ],
        series: [
          {
            name: seriesLabel,
            type: 'line',
            data: points.map((p) => p.momentum),
            connectNulls: false,
            smooth: false,
            showSymbol: points.length < 3 || points.some((point) => point.momentum === null),
            symbol: 'circle',
            symbolSize: 7,
            lineStyle: { width: 2.5, color: chartColors.coral },
            itemStyle: {
              color: chartColors.coral,
              borderWidth: 2,
              borderColor: chartColors.background,
            },
            areaStyle: {
              color: {
                type: 'linear',
                x: 0,
                y: 0,
                x2: 0,
                y2: 1,
                colorStops: [
                  { offset: 0, color: 'rgba(196,72,55,0.18)' },
                  { offset: 1, color: 'rgba(196,72,55,0.005)' },
                ],
              },
            },
            emphasis: { focus: 'series' },
          },
        ],
      },
      { notMerge: true },
    )
  }, [points, name, allowNegative, precision, seriesLabel])
  return (
    <div
      ref={element}
      className="trend-chart"
      role="img"
      aria-label={`${name}历史动量折线图，支持拖动底部滑块缩放`}
    />
  )
}
