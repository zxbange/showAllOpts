import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors as colors } from '../lib/chartTheme'
import type { BreadthPoint } from '../types/breadth'

use([
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  AriaComponent,
  CanvasRenderer,
])
export type BreadthView = 'counts' | 'ratios' | 'spread' | 'strength' | 'sentiment'

export function BreadthChart({
  points,
  period,
  view,
}: {
  points: BreadthPoint[]
  period: number
  view: BreadthView
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)
  useEffect(() => {
    if (!ref.current) return
    chart.current = init(ref.current)
    const observer = new ResizeObserver(() => chart.current?.resize())
    observer.observe(ref.current)
    return () => {
      observer.disconnect()
      chart.current?.dispose()
      chart.current = null
    }
  }, [])
  useEffect(() => {
    const metrics = points.map((p) => p.periods.find((m) => m.period === period))
    const unit = view === 'counts' ? '只' : view === 'ratios' ? '%' : '点'
    const series =
      view === 'counts' || view === 'ratios'
        ? [
            {
              name: '新高',
              type: 'line',
              data: metrics.map((m) => (view === 'counts' ? m?.high_count : m?.high_ratio)),
              color: colors.coral,
            },
            {
              name: '新低',
              type: 'line',
              data: metrics.map((m) => (view === 'counts' ? m?.low_count : m?.low_ratio)),
              color: colors.jade,
            },
          ]
        : [
            {
              name:
                view === 'spread'
                  ? '新高－新低比例差'
                  : view === 'strength'
                    ? '加权市场强度'
                    : '情绪加权值',
              type: view === 'spread' ? 'bar' : 'line',
              data:
                view === 'spread' ? metrics.map((m) => m?.spread_pp) : points.map((p) => p[view]),
              color: view === 'sentiment' ? colors.gold : colors.coral,
              markLine: {
                silent: true,
                symbol: 'none',
                label: { show: false },
                lineStyle: { color: colors.muted, type: 'dashed' },
                data: [{ yAxis: 0 }],
              },
            },
          ]
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 550,
        aria: { enabled: true, description: `${period} 日微盘新高新低及强度的历史趋势` },
        legend: { top: 2, textStyle: { color: colors.muted } },
        grid: { left: 12, right: 24, top: 46, bottom: 70, containLabel: true },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: colors.background,
          borderColor: colors.border,
          textStyle: { color: colors.text },
          valueFormatter: (v: unknown) =>
            v == null ? '暂无数据' : Number(v).toFixed(view === 'counts' ? 0 : 2) + unit,
        },
        xAxis: {
          type: 'category',
          data: points.map((p) => p.date),
          boundaryGap: view === 'spread',
          axisLabel: { color: colors.muted, hideOverlap: true },
          axisLine: { lineStyle: { color: colors.border } },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          axisLabel: { color: colors.muted, formatter: '{value}' + unit },
          splitLine: { lineStyle: { color: colors.grid, type: 'dashed' } },
        },
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: false },
          {
            type: 'slider',
            height: 22,
            bottom: 8,
            borderColor: 'transparent',
            backgroundColor: colors.surface,
            fillerColor: colors.coralFill,
            textStyle: { color: colors.muted },
          },
        ],
        series: series.map((s) => ({
          ...s,
          smooth: false,
          connectNulls: false,
          showSymbol: points.length < 3,
          lineStyle: { width: 2 },
          areaStyle: { opacity: 0.04 },
          barMaxWidth: 12,
        })),
      },
      true,
    )
  }, [points, period, view])
  return (
    <div
      className="breadth-chart"
      ref={ref}
      role="img"
      aria-label={`${period}日 ${view} 历史图表`}
    />
  )
}
