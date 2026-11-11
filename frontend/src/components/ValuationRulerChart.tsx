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
import type { ValuationHistory } from '../types/valuationRuler'
import { valuationChartOption } from '../lib/valuationRuler'
use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
])
export function ValuationRulerChart({ data, days }: { data: ValuationHistory; days: number }) {
  const root = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)
  useEffect(() => {
    if (!root.current) return
    chart.current = init(root.current)
    const observer = new ResizeObserver(() => chart.current?.resize())
    observer.observe(root.current)
    return () => {
      observer.disconnect()
      chart.current?.dispose()
      chart.current = null
    }
  }, [])
  useEffect(() => {
    chart.current?.setOption(valuationChartOption(data, days), { notMerge: true })
  }, [data, days])
  return (
    <div
      ref={root}
      className="valuation-chart"
      role="img"
      aria-label="估值标尺双轴图：左轴沪深300指数，右轴成分股平均PE-TTM及全历史P20、P80，缺失日期断开"
    />
  )
}
