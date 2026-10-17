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
import type { CrowdingHistory } from '../types/crowding'

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

export function CrowdingChart({
  data,
  showSmooth,
}: {
  data: CrowdingHistory
  showSmooth: boolean
}) {
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
    const points = data.points
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 700,
        aria: {
          enabled: true,
          description: '前百分之五股票成交额占全市场成交额的比例，按交易日显示。',
        },
        color: ['#8edbd6', '#edc988'],
        grid: { left: 16, right: 26, top: 60, bottom: 80, containLabel: true },
        legend: { top: 6, textStyle: { color: '#a8bcb3' } },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: '#24342c',
          borderColor: '#46584b',
          textStyle: { color: '#f0f5ec' },
          valueFormatter: (value: unknown) =>
            value == null ? '暂无记录' : Number(value).toFixed(2) + '%',
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((p) => p.date),
          axisLabel: { color: '#88978e', hideOverlap: true },
          axisLine: { lineStyle: { color: '#344038' } },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          min: (value: { min: number }) => Math.max(0, Math.floor(value.min - 2)),
          max: (value: { max: number }) => Math.min(100, Math.ceil(value.max + 2)),
          axisLabel: { color: '#88978e', formatter: '{value}%' },
          splitLine: { lineStyle: { color: '#2b3730', type: 'dashed' } },
        },
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: false },
          {
            type: 'slider',
            bottom: 12,
            height: 24,
            borderColor: 'transparent',
            fillerColor: 'rgba(142,219,214,0.12)',
            textStyle: { color: '#88978e' },
            handleStyle: { color: '#8edbd6' },
          },
        ],
        series: [
          {
            name: '原始占比',
            type: 'line',
            data: points.map((p) => p.ratio * 100),
            smooth: false,
            connectNulls: false,
            showSymbol: points.length < 3,
            lineStyle: { width: 2 },
            itemStyle: { color: '#8edbd6' },
            areaStyle: { opacity: 0.1 },
            markPoint: {
              symbolSize: 48,
              label: {
                formatter: (p: { value: number }) => Number(p.value).toFixed(1) + '%',
                fontSize: 10,
              },
              data: points.length
                ? [
                    {
                      coord: [
                        points[points.length - 1].date,
                        points[points.length - 1].ratio * 100,
                      ],
                      value: points[points.length - 1].ratio * 100,
                    },
                  ]
                : [],
            },
            markLine: {
              silent: true,
              symbol: 'none',
              label: { position: 'insideEndTop', color: '#a8bcb3', formatter: '区间均值' },
              lineStyle: { color: '#90a49a', type: 'dashed' },
              data:
                data.summary.average_ratio == null
                  ? []
                  : [{ yAxis: data.summary.average_ratio * 100 }],
            },
          },
          ...(showSmooth
            ? [
                {
                  name: 'EMA 10 平滑',
                  type: 'line',
                  data: points.map((p) => p.smoothed_ratio * 100),
                  smooth: false,
                  connectNulls: false,
                  showSymbol: false,
                  lineStyle: { width: 2.5, color: '#edc988' },
                  itemStyle: { color: '#edc988' },
                },
              ]
            : []),
        ],
      },
      { notMerge: true },
    )
  }, [data, showSmooth])
  return (
    <div
      ref={element}
      className="trend-chart"
      role="img"
      aria-label="抱团拥挤度历史折线图，支持缩放"
    />
  )
}
