import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
  MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import type { ShortBar } from '../types/shortTerm'

use([
  CandlestickChart,
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
  MarkLineComponent,
  CanvasRenderer,
])
const periods = [5, 10, 20, 30, 60]
const colors = ['#a66e0a', '#497a9c', '#98518f', '#2d826c', '#755f53']
export function ShortTermChart({
  bars,
  name,
  signalDate,
}: {
  bars: ShortBar[]
  name: string
  signalDate?: string
}) {
  const element = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!element.current) return
    const chart = init(element.current)
    const dates = bars.map((b) => b.date)
    chart.setOption({
      textStyle: { color: chartColors.text, fontSize: 13 },
      animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      animationDuration: 450,
      aria: {
        enabled: true,
        description: `${name}近${bars.length}个交易日的前复权日K线、均线及成交量。${signalDate ? `信号日${signalDate}，` : ''}行情截至${bars.at(-1)?.date || '暂无数据'}。阳线红色，阴线绿色。`,
      },
      legend: {
        type: 'scroll',
        top: 6,
        data: periods.map((n) => `MA${n}`),
        textStyle: { color: chartColors.muted, fontSize: 12 },
        itemWidth: 14,
        itemHeight: 2,
      },
      grid: [
        { left: 58, right: 16, top: 42, height: '52%' },
        { left: 58, right: 16, top: '73%', height: '13%' },
      ],
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        axisPointer: { type: 'cross' },
        confine: true,
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [0, 1].map((i) => ({
        type: 'category',
        data: dates,
        gridIndex: i,
        axisLabel: {
          show: i === 1,
          hideOverlap: true,
          formatter: (v: string) => v.slice(5),
          color: chartColors.muted,
        },
        axisLine: { lineStyle: { color: '#e7d9cf' } },
        axisTick: { show: false },
      })),
      yAxis: [
        {
          scale: true,
          splitLine: { lineStyle: { color: '#f0e8e2' } },
          axisLabel: { color: chartColors.muted },
        },
        {
          gridIndex: 1,
          scale: true,
          splitNumber: 2,
          name: '万股',
          nameTextStyle: { color: chartColors.muted },
          splitLine: { show: false },
          axisLabel: { color: chartColors.muted, fontSize: 12 },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1] },
        {
          type: 'slider',
          xAxisIndex: [0, 1],
          bottom: 0,
          height: 18,
          borderColor: '#eaded4',
          fillerColor: 'rgba(196,76,54,.10)',
        },
      ],
      series: [
        {
          name: '日K线',
          type: 'candlestick',
          data: bars.map((b) => [b.open, b.close, b.low, b.high]),
          markLine: {
            silent: true,
            symbol: ['none', 'none'],
            lineStyle: { color: '#755f53', type: 'dashed' },
            label: { formatter: '信号日', position: 'insideEndTop' },
            data: signalDate && dates.includes(signalDate) ? [{ xAxis: signalDate }] : [],
          },
          itemStyle: {
            color: '#c94c39',
            color0: '#29927a',
            borderColor: '#c94c39',
            borderColor0: '#29927a',
          },
        },
        ...periods.map((n, i) => ({
          name: `MA${n}`,
          type: 'line',
          data: bars.map((b) => b.ma[String(n)]),
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 1.3, color: colors[i] },
          itemStyle: { color: colors[i] },
        })),
        {
          name: '成交量（万股）',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: bars.map((b) => ({
            value: b.volume / 1e4,
            itemStyle: { color: b.close >= b.open ? '#dc8979' : '#6db6a6' },
          })),
        },
      ],
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(element.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [bars, name, signalDate])
  return (
    <div
      ref={element}
      className="short-chart"
      role="img"
      aria-label={`${name}近30个交易日K线及均线${signalDate ? `，信号日${signalDate}` : ''}，行情截至${bars.at(-1)?.date || '暂无数据'}`}
    />
  )
}
