import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
use([
  BarChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
  CanvasRenderer,
])

export function OwnershipChart({
  dates,
  values,
  counts,
  title,
  holders = false,
}: {
  dates: string[]
  values: (number | null)[]
  counts?: (number | null)[]
  title: string
  holders?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ref.current) return
    const chart = init(ref.current)
    chart.setOption({
      textStyle: { color: chartColors.text, fontSize: 13 },
      animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      aria: { enabled: true, description: title },
      color: ['#c94835', '#31867a'],
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        confine: true,
        valueFormatter: (v: unknown) =>
          v == null ? '未披露' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 3 }),
      },
      legend: { top: 0, type: 'scroll', textStyle: { color: chartColors.muted, fontSize: 12 } },
      grid: { left: 20, right: 25, top: 55, bottom: 65, containLabel: true },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { color: chartColors.muted, hideOverlap: true },
      },
      yAxis: [
        {
          type: 'value',
          name: holders ? 'A股股东（户）' : '市值（亿元）',
          scale: holders,
          axisLabel: { color: chartColors.muted },
          nameTextStyle: { color: chartColors.muted },
        },
        ...(counts
          ? [
              {
                type: 'value',
                name: '产品/账户数',
                minInterval: 1,
                axisLabel: { color: chartColors.muted },
                nameTextStyle: { color: chartColors.muted },
              },
            ]
          : []),
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          name: holders ? 'A股股东户数' : '披露 / 估算市值',
          type: holders ? 'line' : 'bar',
          data: values,
          connectNulls: false,
          barMaxWidth: 28,
          itemStyle: { borderRadius: [4, 4, 0, 0] },
        },
        ...(counts
          ? [
              {
                name: '产品 / 账户数',
                type: 'line',
                yAxisIndex: 1,
                data: counts,
                connectNulls: false,
              },
            ]
          : []),
      ],
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(ref.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [dates, values, counts, title, holders])
  return <div ref={ref} className="ownership-chart" role="img" aria-label={title} />
}
