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

export function TrendChart({ points, name }: { points: HistoryPoint[]; name: string }) {
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
          backgroundColor: '#24342c',
          borderColor: '#46584b',
          textStyle: { color: '#f0f5ec', fontSize: 12 },
          valueFormatter: (value: unknown) =>
            value == null || value === '-' ? '暂无记录' : Number(value).toFixed(2),
          axisPointer: { type: 'line', lineStyle: { color: '#a9cd94', type: 'dashed' } },
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((p) => p.date),
          axisLine: { lineStyle: { color: '#344038' } },
          axisTick: { show: false },
          axisLabel: {
            color: '#88978e',
            fontSize: 11,
            hideOverlap: true,
            formatter: (value: string) => value.slice(5).replace('-', '/'),
          },
        },
        yAxis: {
          type: 'value',
          min: 0,
          splitNumber: 4,
          axisLabel: { color: '#88978e', fontSize: 11 },
          splitLine: { lineStyle: { color: '#2b3730', type: 'dashed' } },
        },
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: false, moveOnMouseWheel: false },
          {
            type: 'slider',
            height: 22,
            bottom: 12,
            borderColor: 'transparent',
            backgroundColor: '#16211b',
            fillerColor: 'rgba(166,209,139,0.10)',
            dataBackground: { lineStyle: { color: '#5d785e' }, areaStyle: { color: '#314736' } },
            selectedDataBackground: {
              lineStyle: { color: '#add78e' },
              areaStyle: { color: '#5c7a4e' },
            },
            handleStyle: { color: '#afd391', borderColor: '#afd391' },
            textStyle: { color: '#9ca99e' },
          },
        ],
        series: [
          {
            name: '动量热力值',
            type: 'line',
            data: points.map((p) => p.momentum),
            connectNulls: false,
            smooth: false,
            showSymbol: points.length < 3,
            symbol: 'circle',
            symbolSize: 7,
            lineStyle: { width: 2.5, color: '#b7e895' },
            itemStyle: { color: '#b7e895', borderWidth: 2, borderColor: '#18251c' },
            areaStyle: {
              color: {
                type: 'linear',
                x: 0,
                y: 0,
                x2: 0,
                y2: 1,
                colorStops: [
                  { offset: 0, color: 'rgba(177,225,139,0.24)' },
                  { offset: 1, color: 'rgba(177,225,139,0.005)' },
                ],
              },
            },
            emphasis: { focus: 'series' },
          },
        ],
      },
      { notMerge: true },
    )
  }, [points, name])
  return (
    <div
      ref={element}
      className="trend-chart"
      role="img"
      aria-label={`${name}历史动量折线图，支持拖动底部滑块缩放`}
    />
  )
}
