import { useEffect, useRef } from 'react'
import { init, use } from 'echarts/core'
import { CandlestickChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  DataZoomComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import type { EarningsDetail } from '../types/earnings'

use([
  CandlestickChart,
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  DataZoomComponent,
  AriaComponent,
  CanvasRenderer,
])

export function EarningsChart({ data }: { data: EarningsDetail }) {
  const element = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!element.current) return
    const chart = init(element.current)
    chart.setOption({
      textStyle: { color: chartColors.text, fontSize: 13 },
      animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      aria: { enabled: true, description: '信号附近日线走势' },
      grid: { left: 65, right: 24, top: 40, bottom: 65 },
      tooltip: { trigger: 'axis', renderMode: 'richText', confine: true },
      xAxis: {
        type: 'category',
        data: data.prices.map((p) => p.date),
        axisLabel: { color: chartColors.muted },
      },
      yAxis: {
        scale: true,
        axisLabel: { color: chartColors.muted },
        nameTextStyle: { color: chartColors.muted },
        name: '前复权价格（元）',
        splitLine: { lineStyle: { color: '#f0e9e2' } },
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 20, bottom: 10 }],
      series: [
        {
          type: 'candlestick',
          name: '日线',
          data: data.prices.map((p) => [
            p.open ?? '-',
            p.close ?? '-',
            p.low ?? '-',
            p.high ?? '-',
          ]),
          itemStyle: {
            color: '#c44837',
            color0: '#217b70',
            borderColor: '#c44837',
            borderColor0: '#217b70',
          },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [{ xAxis: data.signal_date }],
            label: { formatter: '信号日', color: '#c44837' },
            lineStyle: { color: '#c44837', type: 'dashed' },
          },
        },
      ],
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(element.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [data])
  return <div ref={element} className="earnings-chart" role="img" aria-label="信号附近日线走势" />
}
