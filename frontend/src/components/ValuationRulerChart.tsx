import { useEChart } from '../hooks/useEChart'
import { useEffect } from 'react'
import { use as registerChartComponents } from 'echarts/core'
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
registerChartComponents([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
])
export function ValuationRulerChart({ data, days }: { data: ValuationHistory; days: number }) {
  const { element: root, chart } = useEChart()
  useEffect(() => {
    chart.current?.setOption(valuationChartOption(data, days), { notMerge: true })
  }, [data, days, chart])
  return (
    <div
      ref={root}
      className="valuation-chart"
      role="img"
      aria-label="估值标尺双轴图：左轴沪深300指数，右轴成分股平均PE-TTM及全历史P20、P80，缺失日期断开"
    />
  )
}
