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
  MarkPointComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import type { MarginPoint } from '../types/margin'

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  AriaComponent,
  CanvasRenderer,
])

export function MarginChart({ points }: { points: MarginPoint[] }) {
  const element = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)
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
    const latest = points.at(-1)
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 400,
        aria: {
          enabled: true,
          description: '近六年全市场平均维持担保比例，240%和150%为观察参考线。',
        },
        grid: { left: 16, right: 24, top: 56, bottom: 80, containLabel: true },
        legend: { top: 6, left: 16, textStyle: { color: chartColors.muted, fontSize: 13 } },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          confine: true,
          backgroundColor: chartColors.background,
          borderColor: chartColors.border,
          textStyle: { color: chartColors.text },
          valueFormatter: (value: unknown) => Number(value).toFixed(2) + '%',
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((point) => point.date),
          axisLabel: { color: chartColors.muted, hideOverlap: true, fontSize: 12 },
          axisLine: { lineStyle: { color: chartColors.border } },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          scale: true,
          min: (range: { min: number }) => Math.floor(Math.min(range.min, 150) / 20) * 20,
          max: (range: { max: number }) => Math.ceil(Math.max(range.max, 240) / 20) * 20,
          axisLabel: { color: chartColors.muted, formatter: '{value}%', fontSize: 12 },
          splitLine: { lineStyle: { color: chartColors.grid, type: 'dashed' } },
        },
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: false },
          {
            type: 'slider',
            bottom: 12,
            height: 24,
            borderColor: 'transparent',
            backgroundColor: chartColors.surface,
            fillerColor: chartColors.coralFill,
            textStyle: { color: chartColors.muted },
            handleStyle: { color: chartColors.coral },
          },
        ],
        series: [
          {
            name: '平均维持担保比例',
            type: 'line',
            data: points.map((point) => point.ratio),
            smooth: false,
            showSymbol: points.length < 3,
            connectNulls: false,
            lineStyle: { width: 2.4, color: chartColors.coral },
            itemStyle: { color: chartColors.coral },
            areaStyle: { color: chartColors.coralFill },
            markLine: {
              silent: true,
              symbol: 'none',
              label: { position: 'insideStartTop', formatter: '{b}', fontSize: 12 },
              data: [
                {
                  name: '240% 参考线',
                  yAxis: 240,
                  lineStyle: { color: chartColors.gold, type: 'dashed' },
                  label: { color: chartColors.gold },
                },
                {
                  name: '150% 参考线',
                  yAxis: 150,
                  lineStyle: { color: chartColors.muted, type: 'dashed' },
                  label: { color: chartColors.muted },
                },
              ],
            },
            markPoint: {
              symbol: 'circle',
              symbolSize: 8,
              itemStyle: { color: chartColors.coral },
              label: {
                position: 'left',
                distance: 10,
                color: chartColors.text,
                backgroundColor: chartColors.background,
                borderRadius: 4,
                padding: [5, 7],
                formatter: () => '最新 ' + (latest?.ratio.toFixed(2) ?? '—') + '%',
              },
              data: latest ? [{ coord: [latest.date, latest.ratio], value: latest.ratio }] : [],
            },
          },
        ],
      },
      { notMerge: true },
    )
  }, [points])
  return (
    <div
      ref={element}
      className="trend-chart margin-chart"
      role="img"
      aria-label="近六年两融平均维持担保比例趋势，含240%和150%参考线，支持缩放"
    />
  )
}
