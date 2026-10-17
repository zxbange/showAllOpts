import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import { chartSeries } from '../lib/etf'
import type { ETFHistory } from '../types/etf'

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
])

export function ETFChart({
  data,
  relative,
  selected,
}: {
  data: ETFHistory
  relative: boolean
  selected: string | null
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
    const { dates, series } = chartSeries(data, relative, selected)
    chart.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 650,
        aria: { enabled: true, description: relative ? 'ETF份额变化率曲线' : 'ETF总份额曲线' },
        grid: { left: 16, right: 24, top: 70, bottom: 80, containLabel: true },
        legend: {
          type: 'scroll',
          top: 4,
          selectedMode: false,
          textStyle: { color: chartColors.muted },
          pageTextStyle: { color: chartColors.muted },
        },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: chartColors.background,
          borderColor: chartColors.border,
          textStyle: { color: chartColors.text },
          valueFormatter: (value: unknown) =>
            value == null ? '暂无记录' : Number(value).toFixed(2) + (relative ? '%' : ' 亿份'),
        },
        xAxis: {
          type: 'category',
          data: dates,
          boundaryGap: false,
          axisLine: { lineStyle: { color: chartColors.border } },
          axisLabel: { color: chartColors.muted, hideOverlap: true },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          name: relative ? '份额变化率（%）' : '总份额（亿份）',
          nameTextStyle: { color: chartColors.muted },
          axisLabel: { color: chartColors.muted },
          splitLine: { lineStyle: { color: chartColors.grid } },
        },
        dataZoom: [
          { type: 'inside', zoomOnMouseWheel: 'ctrl' },
          {
            type: 'slider',
            bottom: 12,
            height: 24,
            borderColor: chartColors.border,
            backgroundColor: chartColors.surface,
            fillerColor: chartColors.coralFill,
            textStyle: { color: chartColors.muted },
            handleStyle: { color: chartColors.coral },
            dataBackground: {
              lineStyle: { color: chartColors.muted },
              areaStyle: { color: chartColors.surface },
            },
          },
        ],
        series: series.map((item) => ({
          name: item.name,
          type: 'line',
          data: item.values,
          connectNulls: false,
          smooth: false,
          showSymbol: dates.length < 100,
          symbolSize: 5,
          lineStyle: { width: 2.5 },
          itemStyle: { color: item.color },
          emphasis: { focus: 'series' },
        })),
      },
      true,
    )
  }, [data, relative, selected])
  return (
    <div
      ref={element}
      className="etf-chart"
      role="img"
      aria-label={relative ? 'ETF份额变化率曲线' : 'ETF总份额曲线'}
    />
  )
}
