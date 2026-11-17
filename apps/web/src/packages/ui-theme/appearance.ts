export const THEME_CHOICES = ['system', 'dark', 'light', 'dusk', 'graphite', 'carbon', 'midnight', 'ocean', 'forest', 'nebula', 'ember', 'rose', 'sand', 'lavender', 'sky', 'terminal', 'workbench', 'synthwave', 'pixel', 'typewriter'] as const
export type ThemeChoice = typeof THEME_CHOICES[number]
export type Phosphor = 'green' | 'amber' | 'cyan' | 'white' | 'red' | 'violet' | 'pink' | 'blue'
export type AccentChoice = 'theme' | 'coral' | 'violet' | 'blue' | 'mint' | 'rose' | 'gold' | 'custom'

export const ACCENTS: { id: Exclude<AccentChoice, 'custom'>; label: string; color?: string }[] = [
  { id: 'theme', label: 'Theme default' },
  { id: 'coral', label: 'Coral', color: '#f27858' }, { id: 'violet', label: 'Violet', color: '#a888ff' },
  { id: 'blue', label: 'Blue', color: '#59a7f9' }, { id: 'mint', label: 'Mint', color: '#51c9a8' },
  { id: 'rose', label: 'Rose', color: '#ed82ad' }, { id: 'gold', label: 'Gold', color: '#e6ae43' },
]
export type EffectChoice = 'none' | 'aurora' | 'matrix' | 'scanlines' | 'grid' | 'stars'
export const COLOR_FIELDS = ['background', 'sidebar', 'surface', 'text', 'bubble', 'accent'] as const
export type ColorField = typeof COLOR_FIELDS[number]
export type ThemeColors = Partial<Record<ColorField, string>>
export type ThemePalettes = Partial<Record<ThemeChoice, ThemeColors>>
export interface ThemeStyle { shape?: 'square' | 'rounded'; font?: 'sans' | 'serif' | 'mono'; art?: boolean }
export type ThemeStyles = Partial<Record<ThemeChoice, ThemeStyle>>

export const TEXT_SIZES = { sm: .92, md: 1, lg: 1.08, xl: 1.16 } as const
export type TextSize = keyof typeof TEXT_SIZES
export type Density = 'compact' | 'comfortable' | 'spacious'
export const READING_WIDTHS = { narrow: 660, standard: 780, wide: 980 } as const
export type ReadingWidth = keyof typeof READING_WIDTHS
export const UI_FONTS = {
  system: '',
  rounded: 'ui-rounded, "SF Pro Rounded", "Nunito", system-ui, sans-serif',
  humanist: '"Avenir Next", Avenir, "Segoe UI Variable Text", "Segoe UI", Seravek, Ubuntu, system-ui, sans-serif',
  serif: '"Iowan Old Style", Charter, "Palatino Linotype", Georgia, serif',
  mono: '"SF Mono", "JetBrains Mono", "Cascadia Code", Menlo, Consolas, monospace',
} as const
export type UiFont = keyof typeof UI_FONTS
export type UserStyle = 'bubble' | 'plain'

export interface Appearance {
  textSize: TextSize
  density: Density
  width: ReadingWidth
  font: UiFont
  userStyle: UserStyle
  theme: ThemeChoice
  phosphor: Phosphor
  accent: AccentChoice
  customAccent: string
  effect: EffectChoice
  intensity: number // 0..100
  palettes: ThemePalettes
  paletteSeeds: Partial<Record<ThemeChoice, string>>
  styles: ThemeStyles
}

export const APPEARANCE_KEY = 'vesper.appearance'
const HEX = /^#[\da-f]{6}$/i
const PHOSPHORS: Phosphor[] = ['green', 'amber', 'cyan', 'white', 'red', 'violet', 'pink', 'blue']
export const EFFECTS: { id: EffectChoice; label: string; hint: string }[] = [
  { id: 'none', label: 'None', hint: 'Plain background' },
  { id: 'aurora', label: 'Aurora', hint: 'Slow, soft color glow' },
  { id: 'matrix', label: 'Code rain', hint: 'Falling glyphs' },
  { id: 'scanlines', label: 'CRT', hint: 'Scanlines, vignette, flicker' },
  { id: 'grid', label: 'Grid', hint: 'Moving horizon grid' },
  { id: 'stars', label: 'Starfield', hint: 'Drifting stars' },
]

const reducedMotion = (): boolean =>
  typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

export const DEFAULT_APPEARANCE: Appearance = { textSize: 'md', density: 'comfortable', width: 'standard', font: 'system', userStyle: 'bubble', theme: 'system', phosphor: 'green', accent: 'theme', customAccent: '#a888ff', effect: 'none', intensity: 55, palettes: {}, paletteSeeds: {}, styles: {} }

function validPalettes(value: unknown): ThemePalettes {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result: ThemePalettes = {}
  for (const theme of THEME_CHOICES) {
    const raw = (value as Record<string, unknown>)[theme]
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const colors: ThemeColors = {}
    for (const field of COLOR_FIELDS) {
      const color = (raw as Record<string, unknown>)[field]
      if (typeof color === 'string' && HEX.test(color)) colors[field] = color.toLowerCase()
    }
    if (Object.keys(colors).length) result[theme] = colors
  }
  return result
}

function validSeeds(value: unknown): Partial<Record<ThemeChoice, string>> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result: Partial<Record<ThemeChoice, string>> = {}
  for (const theme of THEME_CHOICES) {
    const seed = (value as Record<string, unknown>)[theme]
    if (typeof seed === 'string' && HEX.test(seed)) result[theme] = seed.toLowerCase()
  }
  return result
}

function validStyles(value: unknown): ThemeStyles {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result: ThemeStyles = {}
  for (const theme of THEME_CHOICES) {
    const raw = (value as Record<string, unknown>)[theme]
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const item = raw as Record<string, unknown>
    const style: ThemeStyle = {}
    if (item.shape === 'square' || item.shape === 'rounded') style.shape = item.shape
    if (item.font === 'sans' || item.font === 'serif' || item.font === 'mono') style.font = item.font
    if (typeof item.art === 'boolean') style.art = item.art
    if (Object.keys(style).length) result[theme] = style
  }
  return result
}

/** Change one color without touching the saved palette of another theme. */
export function withThemeColor(palettes: ThemePalettes, theme: ThemeChoice, field: ColorField, color: string | null): ThemePalettes {
  const next = { ...palettes }
  const colors = { ...next[theme] }
  if (color && HEX.test(color)) colors[field] = color.toLowerCase()
  else delete colors[field]
  if (Object.keys(colors).length) next[theme] = colors
  else delete next[theme]
  return next
}

export function loadAppearance(): Appearance {
  try {
    const raw = JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? '{}') as Partial<Appearance>
    const pick = <T extends string>(value: unknown, options: readonly T[], fallback: T): T =>
      options.includes(value as T) ? (value as T) : fallback
    return {
      textSize: pick(raw.textSize, Object.keys(TEXT_SIZES) as TextSize[], 'md'),
      density: pick(raw.density, ['compact', 'comfortable', 'spacious'] as const, 'comfortable'),
      width: pick(raw.width, Object.keys(READING_WIDTHS) as ReadingWidth[], 'standard'),
      font: pick(raw.font, Object.keys(UI_FONTS) as UiFont[], 'system'),
      userStyle: pick(raw.userStyle, ['bubble', 'plain'] as const, 'bubble'),
      theme: THEME_CHOICES.includes(raw.theme as ThemeChoice) ? (raw.theme as ThemeChoice) : DEFAULT_APPEARANCE.theme,
      phosphor: PHOSPHORS.includes(raw.phosphor as Phosphor) ? (raw.phosphor as Phosphor) : 'green',
      accent: raw.accent === 'custom' || ACCENTS.some(a => a.id === raw.accent) ? raw.accent! : 'theme',
      customAccent: typeof raw.customAccent === 'string' && HEX.test(raw.customAccent) ? raw.customAccent : DEFAULT_APPEARANCE.customAccent,
      effect: EFFECTS.some(e => e.id === raw.effect) ? (raw.effect as EffectChoice) : 'none',
      intensity: typeof raw.intensity === 'number' ? Math.min(100, Math.max(0, raw.intensity)) : 55,
      palettes: validPalettes(raw.palettes),
      paletteSeeds: validSeeds(raw.paletteSeeds),
      styles: validStyles(raw.styles),
    }
  } catch {
    return { ...DEFAULT_APPEARANCE }
  }
}

export function saveAppearance(a: Appearance): void {
  try { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(a)) } catch { /* private mode: keep in memory */ }
}

export function resolvedTheme(theme: ThemeChoice): Exclude<ThemeChoice, 'system'> {
  if (theme !== 'system') return theme
  return typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

type RGB = [number, number, number]
const rgb = (hex: string): RGB => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)) as RGB
const hex = (c: RGB): string => `#${c.map(n => Math.round(n).toString(16).padStart(2, '0')).join('')}`
const mix = (a: RGB, b: RGB, amount: number): RGB => a.map((n, i) => n + (b[i]! - n) * amount) as RGB
const luminance = (c: RGB): number => c.map(n => {
  const x = n / 255
  return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4
}).reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i]!, 0)
export function contrastRatio(a: string, b: string): number {
  const [x, y] = [luminance(rgb(a)), luminance(rgb(b))].sort((p, q) => q - p)
  return (x! + 0.05) / (y! + 0.05)
}

/** Adjust chosen colors just enough to remain legible on the theme's main surfaces. */
export function accessibleAccent(color: string, surfaces: string[]): string {
  if (!HEX.test(color)) return DEFAULT_APPEARANCE.customAccent
  const backgrounds = surfaces.filter(s => HEX.test(s))
  if (!backgrounds.length) return color
  const score = (c: string): number => Math.min(...backgrounds.map(bg => contrastRatio(c, bg)))
  if (score(color) >= 4.6) return color
  const anchor = score('#ffffff') >= score('#000000') ? rgb('#ffffff') : rgb('#000000')
  let low = 0; let high = 1
  for (let i = 0; i < 14; i++) {
    const mid = (low + high) / 2
    if (score(hex(mix(rgb(color), anchor, mid))) >= 4.6) high = mid
    else low = mid
  }
  return hex(mix(rgb(color), anchor, high))
}

/** Effects stay off when the OS asks for reduced motion, whatever the saved choice. */
export function activeEffect(a: Appearance): EffectChoice {
  return reducedMotion() ? 'none' : a.effect
}

export function applyAppearance(a: Appearance): void {
  const html = document.documentElement
  html.dataset.theme = resolvedTheme(a.theme)
  html.dataset.density = a.density
  html.dataset.userStyle = a.userStyle
  html.style.zoom = a.textSize === 'md' ? '' : String(TEXT_SIZES[a.textSize])
  html.style.setProperty('--col', `min(calc(100% - 40px), ${READING_WIDTHS[a.width]}px)`)
  html.dataset.phosphor = a.phosphor
  html.dataset.effect = activeEffect(a)
  const overrides = ['--bg', '--bg-side', '--layer-1', '--layer-2', '--layer-3', '--bubble', '--text', '--text-2', '--text-3', '--line', '--line-2', '--ok', '--warn', '--err', '--phos', '--phos-rgb', '--accent', '--accent-2', '--accent-soft', '--accent-ink', '--brand', '--brand-2', '--send-bg', '--send-fg', '--font', '--title-font', '--r-xs', '--r-sm', '--r-md', '--r-lg', '--r-xl', '--r-panel', '--user-shape', 'color-scheme']
  for (const name of overrides) html.style.removeProperty(name)
  delete html.dataset.customPalette
  delete html.dataset.shape
  delete html.dataset.art
  const style = validStyles(a.styles)[a.theme] ?? {}
  if (style.art === false) html.dataset.art = 'off'
  if (style.shape) {
    html.dataset.shape = style.shape
    const sizes = style.shape === 'square' ? ['0px', '0px', '0px', '0px', '0px', '0px'] : ['6px', '12px', '18px', '22px', '26px', '32px']
    ;['--r-xs', '--r-sm', '--r-md', '--r-lg', '--r-xl', '--r-panel'].forEach((name, i) => html.style.setProperty(name, sizes[i]!))
    html.style.setProperty('--user-shape', style.shape === 'square' ? '0px' : '24px 24px 6px 24px')
  }
  if (style.font) {
    const font = { sans: 'system-ui, sans-serif', serif: 'Georgia, serif', mono: 'Menlo, Consolas, monospace' }[style.font]
    html.style.setProperty('--font', font)
    html.style.setProperty('--title-font', font)
  }
  else if (UI_FONTS[a.font]) {
    html.style.setProperty('--font', UI_FONTS[a.font])
    html.style.setProperty('--title-font', UI_FONTS[a.font])
  }
  const palette = validPalettes(a.palettes)[a.theme] ?? {}
  const customSurfaces = !!(palette.background || palette.sidebar || palette.surface || palette.text || palette.bubble)
  const defaults = getComputedStyle(html)
  const defaultText = defaults.getPropertyValue('--text').trim()
  const defaultBg = defaults.getPropertyValue('--bg').trim()
  const set = (name: string, value: string): void => html.style.setProperty(name, value)
  if (customSurfaces) {
    html.dataset.customPalette = 'true'
    if (palette.background) set('--bg', palette.background)
    if (palette.sidebar) set('--bg-side', palette.sidebar)
    if (palette.surface) {
      const ink = palette.text || (HEX.test(defaultText) ? defaultText : '#ffffff')
      set('--layer-1', palette.surface)
      set('--layer-2', hex(mix(rgb(palette.surface), rgb(ink), .08)))
      set('--layer-3', hex(mix(rgb(palette.surface), rgb(ink), .16)))
      set('--bubble', hex(mix(rgb(palette.surface), rgb(ink), .08)))
    }
    if (palette.bubble) set('--bubble', palette.bubble)
    if (palette.text) {
      set('--text', palette.text)
      if (a.theme === 'terminal') {
        set('--phos', palette.text)
        set('--phos-rgb', rgb(palette.text).join(', '))
      }
    }
    set('--line', 'color-mix(in srgb, var(--text) 12%, transparent)')
    set('--line-2', 'color-mix(in srgb, var(--text) 22%, transparent)')
    const bg = palette.background || defaultBg
    const ink = palette.text || defaultText
    if (HEX.test(bg) && HEX.test(ink)) {
      const css = getComputedStyle(html)
      const surfaces = ['--bg', '--bg-side', '--layer-1', '--layer-2', '--bubble'].map(n => css.getPropertyValue(n).trim())
      set('--text-2', accessibleAccent(hex(mix(rgb(ink), rgb(bg), .15)), surfaces))
      set('--text-3', accessibleAccent(hex(mix(rgb(ink), rgb(bg), .28)), surfaces))
    }
    if (HEX.test(bg)) {
      const light = luminance(rgb(bg)) > .35
      set('color-scheme', light ? 'light' : 'dark')
      if (a.theme !== 'terminal') {
        const css = getComputedStyle(html)
        const surfaces = ['--bg', '--bg-side', '--layer-1', '--layer-2'].map(n => css.getPropertyValue(n).trim())
        for (const [name, color] of Object.entries(light
          ? { '--ok': '#15803d', '--warn': '#b45309', '--err': '#b91c1c' }
          : { '--ok': '#4ade80', '--warn': '#fbbf24', '--err': '#f87171' })) set(name, accessibleAccent(color, surfaces))
      }
    }
  }
  if (a.theme !== 'terminal') {
    const preset = a.accent === 'custom' ? a.customAccent : ACCENTS.find(c => c.id === a.accent)?.color
    const chosen = palette.accent || (a.accent !== 'theme' ? preset : undefined)
    if (chosen && HEX.test(chosen)) {
      const css = getComputedStyle(html)
      const surfaces = ['--bg', '--bg-side', '--layer-1', '--layer-2'].map(n => css.getPropertyValue(n).trim())
      // Studio colors are exact; legacy preset accents retain their readability adjustment.
      const color = palette.accent ? chosen : accessibleAccent(chosen, surfaces)
      const ink = contrastRatio(color, '#000000') >= contrastRatio(color, '#ffffff') ? '#000000' : '#ffffff'
      const anchor = ink === '#000000' ? rgb('#ffffff') : rgb('#000000')
      const secondary = hex(mix(rgb(color), anchor, 0.28))
      const values: Record<string, string> = { '--accent': color, '--accent-2': secondary,
        '--accent-soft': `color-mix(in srgb, ${color} 14%, transparent)`, '--accent-ink': ink,
        '--brand': color, '--brand-2': secondary, '--send-bg': color, '--send-fg': ink }
      for (const [name, value] of Object.entries(values)) set(name, value)
    }
  }
  html.style.setProperty('--fx', String(a.intensity / 100))
  const ua = navigator.userAgent
  html.classList.toggle('is-desktop', /Electron\//.test(ua))
  html.classList.toggle('is-mac', /Macintosh|Mac OS X/.test(ua))
  html.classList.toggle('is-win', /Windows/.test(ua))
}
