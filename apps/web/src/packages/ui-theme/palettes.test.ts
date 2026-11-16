import { describe, expect, it } from 'vitest'
import { contrastRatio, THEME_CHOICES, type ThemeColors } from './appearance'
import { paletteFromColor, samePalette, THEME_PALETTES } from './palettes'

function checkContrast(colors: ThemeColors): void {
  const surfaces = [colors.background, colors.sidebar, colors.surface, colors.bubble].filter((c): c is string => !!c)
  expect(surfaces.length).toBeGreaterThanOrEqual(3)
  for (const bg of surfaces) {
    expect(contrastRatio(colors.text!, bg), `text ${colors.text} on ${bg}`).toBeGreaterThanOrEqual(4.5)
    if (colors.accent) expect(contrastRatio(colors.accent, bg), `accent ${colors.accent} on ${bg}`).toBeGreaterThanOrEqual(4.5)
  }
}

describe('coordinated theme palettes', () => {
  it('has two readable, distinct options for every theme', () => {
    for (const theme of THEME_CHOICES) {
      const choices = THEME_PALETTES[theme]
      expect(choices).toHaveLength(2)
      expect(samePalette(choices[0]!.colors, choices[1]!.colors)).toBe(false)
      for (const choice of choices) checkContrast(choice.colors)
    }
  })

  it('generates a complete readable light or dark palette from a single color', () => {
    for (const seed of ['#000000', '#ffffff', '#ff0000', '#00ff00', '#0000ff', '#8b7cff', '#333333']) {
      for (const light of [false, true]) {
        const generated = paletteFromColor(seed, light)
        expect(Object.keys(generated)).toHaveLength(6)
        checkContrast(generated)
        expect(generated.background).not.toBe(generated.surface)
      }
      const terminal = paletteFromColor(seed, false, true)
      expect(Object.keys(terminal)).toHaveLength(4)
      checkContrast(terminal)
    }
    expect(paletteFromColor('invalid', false)).toEqual(paletteFromColor('#8b7cff', false))
  })

  it('recognizes exact preset colors without confusing partial edits', () => {
    const preset = THEME_PALETTES.midnight[0]!.colors
    expect(samePalette(preset, { ...preset })).toBe(true)
    expect(samePalette(preset, { ...preset, accent: '#ffffff' })).toBe(false)
    expect(samePalette(preset, { background: preset.background })).toBe(false)
  })
})
