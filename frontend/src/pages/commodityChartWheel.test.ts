import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('commodity chart gestures', () => {
  it.each(['CommoditiesPage', 'CCICoalPage'])(
    '%s disables wheel zoom/pan while retaining inside gestures and the date slider',
    (page) => {
      const source = readFileSync(new URL(`./${page}.tsx`, import.meta.url), 'utf8')
      expect(source).toMatch(
        /dataZoom:\s*\[\s*\{\s*type:\s*'inside',\s*zoomOnMouseWheel:\s*false,\s*moveOnMouseWheel:\s*false\s*\},\s*\{\s*type:\s*'slider',\s*bottom:\s*8,\s*height:\s*22\s*\}/,
      )
    },
  )
})
