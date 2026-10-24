import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkPointComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import type { SentimentPoint } from '../types/sentiment'

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkPointComponent,
  AriaComponent,
  CanvasRenderer,
])

export function SentimentChart({ points }: { points: SentimentPoint[] }) {
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
    const blue = '#3568d4'
    const latest = points.at(-1)
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 500,
        aria: {
          enabled: true,
          description: '全市场站上20日均线的股票比例。低于20%为冰点，高于80%为过热。',
        },
        grid: { left: 16, right: 24, top: 76, bottom: 80, containLabel: true },
        legend: {
          top: 6,
          left: 16,
          right: 16,
          itemWidth: 22,
          textStyle: { color: chartColors.muted, fontSize: 13 },
        },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          confine: true,
          backgroundColor: chartColors.background,
          borderColor: chartColors.border,
          textStyle: { color: chartColors.text },
          valueFormatter: (v: unknown) => Number(v).toFixed(2) + '%',
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((p) => p.date),
          axisLabel: { color: chartColors.muted, hideOverlap: true },
          axisLine: { lineStyle: { color: chartColors.border } },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          min: 0,
          max: 100,
          interval: 20,
          axisLabel: { formatter: '{value}%', color: chartColors.muted },
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
            fillerColor: 'rgba(53,104,212,0.12)',
            textStyle: { color: chartColors.muted },
            handleStyle: { color: blue },
          },
        ],
        series: [
          {
            name: '站上MA20比例',
            type: 'line',
            data: points.map((p) => p.ratio * 100),
            smooth: false,
            showSymbol: points.length < 3,
            connectNulls: false,
            lineStyle: { width: 2.5 },
            itemStyle: { color: blue },
            areaStyle: { color: blue, opacity: 0.1 },
            z: 3,
            markPoint: {
              symbol: 'circle',
              symbolSize: 9,
              itemStyle: { color: chartColors.gold },
              label: {
                position: 'left',
                distance: 10,
                color: chartColors.text,
                backgroundColor: '#fff5dc',
                borderColor: '#e9bb5b',
                borderWidth: 1,
                borderRadius: 6,
                padding: [6, 8],
                formatter: () => '当前 ' + ((latest?.ratio ?? 0) * 100).toFixed(1) + '%',
              },
              data: latest
                ? [{ coord: [latest.date, latest.ratio * 100], value: latest.ratio * 100 }]
                : [],
            },
            markArea: {
              silent: true,
              label: { show: false },
              data: [
                [{ yAxis: 0, itemStyle: { color: 'rgba(33,123,112,0.05)' } }, { yAxis: 20 }],
                [{ yAxis: 80, itemStyle: { color: 'rgba(196,72,55,0.05)' } }, { yAxis: 100 }],
              ],
            },
          },
          ...[
            { name: '冰点线 20%', value: 20, color: chartColors.jade },
            { name: '过热线 80%', value: 80, color: chartColors.coral },
          ].map((line) => ({
            name: line.name,
            type: 'line',
            data: points.map(() => line.value),
            showSymbol: false,
            lineStyle: { color: line.color, type: 'dashed', width: 1.5 },
            itemStyle: { color: line.color },
            z: 4,
          })),
        ],
      },
      { notMerge: true },
    )
  }, [points])
  return (
    <div
      ref={element}
      className="trend-chart sentiment-chart"
      role="img"
      aria-label="市场情绪历史折线图：站上20日均线股票比例，冰点线20%，过热线80%，支持缩放"
    />
  )
}
