import { graphic, type EChartsType } from 'echarts/core'
import type { GrowthDividendBenchmark } from './growthDividend'

// Share the existing axis gutters with the short references, rather than
// squeezing the plot into a narrow column on phones.
export const growthDividendGrid = { left: 56, right: 52, top: 40, bottom: 78 }

const fontSize = 11
const lineHeight = 14
const labelGap = 6

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

/** Find the closest ordered pair of free positions; equal thresholds keep both short labels. */
function separateLabels(
  targets: number[],
  blocked: Array<[number, number]>,
  min: number,
  max: number,
) {
  const spacing = lineHeight + labelGap
  const clamp = (value: number) => Math.max(min, Math.min(max, value))
  const base = [
    min,
    max,
    ...targets,
    (targets[0] + targets[1] - spacing) / 2,
    (targets[0] + targets[1] + spacing) / 2,
    ...blocked.flat(),
  ]
  const candidates = [...new Set(base.flatMap((y) => [y - spacing, y, y + spacing]).map(clamp))]
    .filter((y) => blocked.every(([top, bottom]) => y <= top || y >= bottom))
    .sort((a, b) => a - b)
  // A collapsed/hidden chart may have no room. Never fall back to overlapping
  // labels; redraw normally when ResizeObserver reports a usable height.
  let result: number[] | null = null
  let best = Infinity
  for (const first of candidates) {
    for (const second of candidates) {
      if (second - first < spacing) continue
      const cost = (first - targets[0]) ** 2 + (second - targets[1]) ** 2
      if (cost < best) {
        result = [first, second]
        best = cost
      }
    }
  }
  return result
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
    const labels = benchmark.levels
      .flatMap((level) =>
        (level.percentile === 'P20/P80' ? ['P80', 'P20'] : [level.percentile]).map(
          (percentile) => ({
            text: `${onLeft ? '创' : '科'} ${percentile === 'P20' ? '0.2' : '0.8'}`,
            y: chart.convertToPixel({ yAxisIndex: 1 }, level.value) as number,
          }),
        ),
      )
      .filter((label) => Number.isFinite(label.y))
      .sort((a, b) => a.y - b.y)
    if (labels.length !== 2) continue
    const blocked = ticks
      .filter((rect) => (onLeft ? rect.x + rect.width <= left : rect.x >= plotRight))
      .map((rect): [number, number] => [
        rect.y - lineHeight / 2 - 3,
        rect.y + rect.height + lineHeight / 2 + 3,
      ])
    const positions = separateLabels(
      labels.map((label) => label.y),
      blocked,
      top + lineHeight / 2,
      plotBottom - lineHeight / 2,
    )
    if (!positions) continue
    labels.forEach((label, index) => {
      const y = positions[index]
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
    })
  }
  return group
}
