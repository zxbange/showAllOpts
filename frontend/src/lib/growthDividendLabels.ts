import { graphic, type EChartsType } from 'echarts/core'
import { formatGrowthDividendPercentile, type GrowthDividendBenchmark } from './growthDividend'

// Share the existing axis gutters with the short references, rather than
// squeezing the plot into a narrow column on phones.
export const growthDividendGrid = { left: 56, right: 56, top: 40, bottom: 78 }

const fontSize = 11
const lineHeight = 14

/** Read actual rendered tick bounds, so collision checks use the same font/layout as the chart. */
export function growthDividendAxisTicks(chart: EChartsType) {
  const texts = new Set<InstanceType<typeof graphic.Text>>()
  for (const element of chart.getZr().storage.getDisplayList(true)) {
    if (element.parent instanceof graphic.Text) texts.add(element.parent)
  }
  return [...texts]
    .filter((text) => /^[+-]?[\d,.]+(?:e[+-]?\d+)?$/i.test(String(text.style.text)))
    .map((text) => {
      const rect = text.getBoundingRect().clone()
      const transform = text.getComputedTransform()
      if (transform) rect.applyTransform(transform)
      return rect
    })
}

/** Closest free label position in its axis gutter; skip a collapsed viewport. */
function nearestLabel(target: number, blocked: Array<[number, number]>, min: number, max: number) {
  if (max < min) return null
  const clamp = (value: number) => Math.max(min, Math.min(max, value))
  const candidates = [min, max, target, ...blocked.flat()]
    .map(clamp)
    .filter((y) => blocked.every(([top, bottom]) => y <= top || y >= bottom))
    .sort((a, b) => Math.abs(a - target) - Math.abs(b - target))
  return candidates[0] ?? null
}

/** Overlay only the outer axis gutters. Reference lines remain on ratio yAxisIndex 1. */
export function growthDividendBenchmarkLabels(
  chart: EChartsType,
  benchmarks: GrowthDividendBenchmark[],
) {
  const group = new graphic.Group({ silent: true })
  const ticks = growthDividendAxisTicks(chart)
  const { left, right, top, bottom } = growthDividendGrid
  const plotRight = chart.getWidth() - right
  const plotBottom = chart.getHeight() - bottom
  for (const benchmark of benchmarks) {
    const onLeft = benchmark.key === 'chinext_dividend_ratio'
    const edge = onLeft ? left : plotRight
    const direction = onLeft ? -1 : 1
    const level = benchmark.levels[0]
    if (!level) continue
    const label = {
      text: `${onLeft ? '创' : '科'} ${formatGrowthDividendPercentile(level.percentile)}`,
      y: chart.convertToPixel({ yAxisIndex: 1 }, level.value) as number,
    }
    if (!Number.isFinite(label.y)) continue
    const blocked = ticks
      .filter((rect) => (onLeft ? rect.x + rect.width <= left : rect.x >= plotRight))
      .map((rect): [number, number] => [
        rect.y - lineHeight / 2 - 3,
        rect.y + rect.height + lineHeight / 2 + 3,
      ])
    const y = nearestLabel(label.y, blocked, top + lineHeight / 2, plotBottom - lineHeight / 2)
    if (y == null) continue
    // The thin connector stays in the 8px gap before numeric ticks, even
    // when the text moves to avoid a tick or a nearly equal reference.
    group.add(
      new graphic.Polyline({
        silent: true,
        z: 8,
        shape: {
          points: [
            [edge, label.y],
            [edge + direction * 4, label.y],
            [edge + direction * 4, y],
            [edge + direction * 7, y],
          ],
        },
        style: { stroke: benchmark.color, lineWidth: 0.8, opacity: 0.4, fill: 'none' },
      }),
    )
    group.add(
      new graphic.Text({
        name: label.text,
        silent: true,
        z: 9,
        style: {
          text: label.text,
          x: edge + direction * 10,
          y,
          align: onLeft ? 'right' : 'left',
          verticalAlign: 'middle',
          fontSize,
          fontFamily: 'sans-serif',
          lineHeight,
          fill: benchmark.color,
          opacity: 0.65,
        },
      }),
    )
  }
  return group
}
