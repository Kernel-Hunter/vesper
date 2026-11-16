import { afterEach, describe, expect, it, vi } from 'vitest'
import { accessibleAccent, activeEffect, applyAppearance, contrastRatio, DEFAULT_APPEARANCE, loadAppearance, saveAppearance, THEME_CHOICES, withThemeColor } from './appearance'

const storage = (): Storage => {
  const values = new Map<string, string>()
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value) },
    removeItem: key => { values.delete(key) }, clear: () => values.clear(), key: i => [...values.keys()][i] ?? null,
    get length() { return values.size } }
}

describe('appearance', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('loads old settings safely and persists every new palette and accent', () => {
    const local = storage()
    vi.stubGlobal('localStorage', local)
    local.setItem('vesper.appearance', JSON.stringify({ theme: 'terminal', phosphor: 'amber', effect: 'stars', intensity: 80 }))
    expect(loadAppearance()).toEqual({ ...DEFAULT_APPEARANCE, theme: 'terminal', phosphor: 'amber', effect: 'stars', intensity: 80 })
    for (const theme of THEME_CHOICES) {
      const value = { ...DEFAULT_APPEARANCE, theme, accent: 'custom' as const, customAccent: '#123456' }
      saveAppearance(value)
      expect(loadAppearance()).toEqual(value)
    }
    local.setItem('vesper.appearance', JSON.stringify({ theme: 'bad', accent: 'bad', customAccent: 'url(evil)', intensity: 1000 }))
    expect(loadAppearance()).toEqual({ ...DEFAULT_APPEARANCE, intensity: 100 })
    local.setItem('vesper.appearance', '{')
    expect(loadAppearance()).toEqual(DEFAULT_APPEARANCE)
  })

  it('saves independent theme colors and styles and rejects invalid saved data', () => {
    const local = storage()
    vi.stubGlobal('localStorage', local)
    let palettes = withThemeColor({}, 'pixel', 'background', '#112233')
    palettes = withThemeColor(palettes, 'pixel', 'accent', '#ABCDEF')
    palettes = withThemeColor(palettes, 'workbench', 'surface', '#d0d0d0')
    const saved = { ...DEFAULT_APPEARANCE, theme: 'pixel' as const, palettes, paletteSeeds: { pixel: '#654321' }, styles: { pixel: { font: 'serif' as const, shape: 'rounded' as const, art: false } } }
    saveAppearance(saved)
    expect(loadAppearance()).toEqual(saved)
    expect(withThemeColor(palettes, 'pixel', 'accent', null)).toEqual({ pixel: { background: '#112233' }, workbench: { surface: '#d0d0d0' } })
    expect(withThemeColor(palettes, 'pixel', 'background', null).workbench).toEqual({ surface: '#d0d0d0' })
    local.setItem('vesper.appearance', JSON.stringify({ palettes: { pixel: { background: 'url(evil)', surface: '#abcdef', bad: '#123456' }, bad: { accent: '#111111' } }, paletteSeeds: { pixel: '#AB1234', workbench: 'invalid', bad: '#ffffff' }, styles: { pixel: { font: 'bad', shape: 'square', art: 'no' } } }))
    expect(loadAppearance().palettes).toEqual({ pixel: { surface: '#abcdef' } })
    expect(loadAppearance().styles).toEqual({ pixel: { shape: 'square' } })
    expect(loadAppearance().paletteSeeds).toEqual({ pixel: '#ab1234' })
  })

  it('keeps custom colors readable on both light and dark surfaces', () => {
    const palettes = [
      ['#101525', '#171f35', '#1a233a', '#24304b'],
      ['#f8f5fc', '#eee8f6', '#f0eaf6', '#e5dced'],
      ['#212121', '#303030', '#2c2c2c', '#383838'],
    ]
    for (const surfaces of palettes) for (const chosen of ['#000000', '#ffffff', '#ff0000', '#00ff00', '#0000ff', '#a888ff']) {
      const color = accessibleAccent(chosen, surfaces)
      for (const surface of surfaces) expect(contrastRatio(color, surface)).toBeGreaterThanOrEqual(4.5)
      const ink = Math.max(contrastRatio(color, '#000000'), contrastRatio(color, '#ffffff'))
      expect(ink).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('applies exact theme colors and details without leaking them into other themes', () => {
    const styles = new Map<string, string>()
    const html = { dataset: {} as Record<string, string>, style: {
      setProperty: (key: string, value: string) => styles.set(key, value), removeProperty: (key: string) => styles.delete(key),
    }, classList: { toggle: vi.fn() } }
    vi.stubGlobal('document', { documentElement: html })
    vi.stubGlobal('navigator', { userAgent: 'test' })
    vi.stubGlobal('getComputedStyle', () => ({ getPropertyValue: () => '#212121' }))
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    const value = { ...DEFAULT_APPEARANCE, theme: 'pixel' as const, palettes: { pixel: { background: '#112233', sidebar: '#223344', surface: '#334455', text: '#eeeeee', bubble: '#445566', accent: '#abcdef' } },
      styles: { pixel: { shape: 'rounded' as const, font: 'serif' as const, art: false } } }
    applyAppearance(value)
    expect(styles.get('--bg')).toBe('#112233')
    expect(styles.get('--bg-side')).toBe('#223344')
    expect(styles.get('--layer-1')).toBe('#334455')
    expect(styles.get('--text')).toBe('#eeeeee')
    expect(styles.get('--bubble')).toBe('#445566')
    expect(styles.get('--accent')).toBe('#abcdef')
    expect(styles.get('--font')).toContain('Georgia')
    expect(styles.get('--r-panel')).toBe('32px')
    expect(html.dataset.art).toBe('off')
    applyAppearance({ ...value, theme: 'workbench' })
    expect(styles.has('--bg')).toBe(false)
    expect(styles.has('--font')).toBe(false)
    expect(styles.has('--r-panel')).toBe(false)
    expect(html.dataset.art).toBeUndefined()
    applyAppearance({ ...DEFAULT_APPEARANCE, theme: 'terminal', palettes: { terminal: { text: '#123456' } } })
    expect(styles.get('--phos')).toBe('#123456')
    expect(styles.get('--phos-rgb')).toBe('18, 52, 86')
  })

  it('applies custom colors, restores the theme default, and keeps terminal phosphors independent', () => {
    const styles = new Map<string, string>()
    const html = { dataset: {} as Record<string, string>, style: {
      setProperty: (key: string, value: string) => styles.set(key, value), removeProperty: (key: string) => styles.delete(key),
    }, classList: { toggle: vi.fn() } }
    vi.stubGlobal('document', { documentElement: html })
    vi.stubGlobal('navigator', { userAgent: 'test' })
    vi.stubGlobal('getComputedStyle', () => ({ getPropertyValue: () => '#212121' }))
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    applyAppearance({ ...DEFAULT_APPEARANCE, theme: 'nebula', accent: 'custom', customAccent: '#112233' })
    expect(html.dataset.theme).toBe('nebula')
    expect(contrastRatio(styles.get('--accent')!, '#212121')).toBeGreaterThanOrEqual(4.5)
    applyAppearance({ ...DEFAULT_APPEARANCE, theme: 'nebula' })
    expect(styles.has('--accent')).toBe(false)
    applyAppearance({ ...DEFAULT_APPEARANCE, theme: 'terminal', accent: 'violet', phosphor: 'amber' })
    expect(html.dataset.phosphor).toBe('amber')
    expect(styles.has('--accent')).toBe(false)
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    expect(activeEffect({ ...DEFAULT_APPEARANCE, effect: 'stars' })).toBe('none')
  })
})
