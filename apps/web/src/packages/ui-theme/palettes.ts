import { accessibleAccent, type ThemeChoice, type ThemeColors } from './appearance'

export interface PaletteOption { label: string; note: string; colors: ThemeColors }
const palette = (label: string, note: string, background: string, sidebar: string, surface: string, text: string, bubble: string, accent: string): PaletteOption =>
  ({ label, note, colors: { background, sidebar, surface, text, bubble, accent } })
const consolePalette = (label: string, note: string, background: string, sidebar: string, surface: string, text: string): PaletteOption =>
  ({ label, note, colors: { background, sidebar, surface, text } })

/* Each pair is chosen for the theme's own material and mood, not shared filters. */
export const THEME_PALETTES: Record<ThemeChoice, readonly PaletteOption[]> = {
  system: [
    palette('Nightfall', 'Cool charcoal', '#171c25', '#202837', '#283246', '#f2f5ff', '#344057', '#b9d2ff'),
    palette('Daybreak', 'Warm porcelain', '#f8f4eb', '#ece5d9', '#fffbf3', '#292720', '#e4d9c9', '#81512c'),
  ],
  dark: [
    palette('Copper Ink', 'Warm graphite', '#1c1c1b', '#262522', '#302d2a', '#f5ede2', '#3c3730', '#ffc083'),
    palette('Blue Steel', 'Cool graphite', '#171d25', '#202b37', '#293645', '#eff5ff', '#354459', '#a7d1ff'),
  ],
  light: [
    palette('Porcelain', 'Cool and crisp', '#f5f7f7', '#e7edef', '#ffffff', '#202d32', '#d9e4e8', '#235a78'),
    palette('Linen', 'Soft and warm', '#f8f2e8', '#eee3d4', '#fffbf4', '#34281f', '#e6d7c5', '#8b4628'),
  ],
  dusk: [
    palette('Evening Star', 'Indigo and starlight', '#15141d', '#1d1b28', '#24222f', '#f3f0fa', '#2c2939', '#f2c46d'),
    palette('Afterglow', 'Plum and rose gold', '#1a1419', '#251c23', '#2c222a', '#fbf0f3', '#352833', '#f0a98c'),
  ],
  graphite: [
    palette('Pencil', 'Neutral and quiet', '#18191b', '#202124', '#26282b', '#eceef1', '#2e3034', '#c7d0dc'),
    palette('Slate', 'Blue-grey steel', '#161a1f', '#1d232a', '#232a33', '#e9eff5', '#2b333d', '#8fb4d9'),
  ],
  carbon: [
    palette('Black', 'True black for OLED', '#000000', '#0b0b0c', '#121214', '#f5f5f6', '#1a1a1d', '#ffffff'),
    palette('Signal', 'Black with a lime signal', '#000000', '#0a0b0a', '#111311', '#f2f5ef', '#191c18', '#c6f36b'),
  ],
  midnight: [
    palette('Moonlit', 'Silver and indigo', '#111628', '#1b2339', '#222d46', '#f1f4ff', '#303c59', '#bcd0ff'),
    palette('Astral', 'Plum night sky', '#1e1932', '#29223e', '#332a49', '#f8efff', '#403450', '#e4bdff'),
  ],
  ocean: [
    palette('Lagoon', 'Tidal turquoise', '#102422', '#17332f', '#20403a', '#ecfff8', '#2a4d45', '#9befd4'),
    palette('Deep Current', 'Navy and ice', '#111e30', '#192b40', '#23394d', '#eef9ff', '#30475b', '#a8dbff'),
  ],
  forest: [
    palette('Pine Canopy', 'Evergreen and mist', '#15231c', '#1e3226', '#283d2f', '#f0f8ea', '#354b39', '#c5e9b0'),
    palette('Autumn Trail', 'Bark and brass', '#252016', '#342b1c', '#403522', '#fff5e3', '#4d402a', '#f2d18e'),
  ],
  nebula: [
    palette('Orchid Dust', 'Violet and rose', '#261a30', '#34243f', '#402e4b', '#fff0fa', '#4f3b58', '#ffc0ea'),
    palette('Ion Cloud', 'Blue and violet', '#191c35', '#252947', '#303455', '#f4f2ff', '#3c4061', '#c8c2ff'),
  ],
  ember: [
    palette('Copper Hearth', 'Burnished copper', '#281a17', '#38231e', '#462c25', '#fff1e5', '#52372d', '#ffc5a0'),
    palette('Brass & Soot', 'Dark leather', '#201d19', '#302a22', '#3c352a', '#fff3de', '#4b4131', '#efd093'),
  ],
  rose: [
    palette('Velvet Wine', 'Burgundy and blush', '#2a1822', '#3a222e', '#482c38', '#fff0f5', '#573846', '#ffc3da'),
    palette('Mauve Silk', 'Dusty mauve', '#261e2a', '#352a38', '#423546', '#fbeefa', '#504253', '#efc4ec'),
  ],
  sand: [
    palette('Olive Paper', 'Sage and parchment', '#f5f2e4', '#e7e5d2', '#fcfaef', '#2c3022', '#dedfc9', '#53652d'),
    palette('Terracotta', 'Clay and cream', '#fbf3e8', '#f0e2d2', '#fffaf2', '#35251d', '#e9d5c2', '#914424'),
  ],
  lavender: [
    palette('Lilac Milk', 'Pale lilac', '#f8f4fc', '#eee6f5', '#fffbff', '#30243a', '#e3d5eb', '#714692'),
    palette('Rose Petal', 'Pink stationery', '#fcf4f7', '#f4e5eb', '#fffbfd', '#37252e', '#ecd8e1', '#8d3e63'),
  ],
  sky: [
    palette('Clear Air', 'Blue and white', '#f0f8fb', '#deedf3', '#fbfeff', '#1c303a', '#d3e6ed', '#1e607e'),
    palette('Morning Fog', 'Slate and silver', '#f3f6f6', '#e3ebec', '#fbfdfd', '#263337', '#d7e2e4', '#3f626c'),
  ],
  terminal: [
    consolePalette('Deep Green', 'Forest phosphor', '#030906', '#0a1c12', '#11291b', '#8effb2'),
    consolePalette('Amber Night', 'Warm phosphor', '#0c0702', '#201508', '#30200e', '#ffd086'),
  ],
  workbench: [
    palette('Slate Desktop', 'Teal title bars', '#e2e9e7', '#ccd9d5', '#f5faf7', '#1c302d', '#c4d5cf', '#145b54'),
    palette('Platinum', 'Aubergine chrome', '#eae6e2', '#d8d0d3', '#fbf7f5', '#30242c', '#d6c9d0', '#6d3960'),
  ],
  synthwave: [
    palette('Electric Sunset', 'Tangerine and pink', '#291323', '#3b1d32', '#4a2840', '#fff0f5', '#57334a', '#ffc3a3'),
    palette('Ultraviolet', 'Violet and cyan', '#18132e', '#251d40', '#332a50', '#f5efff', '#41365f', '#aeeaff'),
  ],
  pixel: [
    palette('Pocket Console', 'Moss and lime', '#172119', '#223125', '#2d3d2d', '#f3f9e8', '#3b4b38', '#d7f4a6'),
    palette('Candy Cabinet', 'Cherry and cream', '#281525', '#3b2037', '#492b43', '#fff0fa', '#57374f', '#ffc0dc'),
  ],
  typewriter: [
    palette('Blue Carbon', 'Blue ink on paper', '#f2f2e9', '#e2e6de', '#fffef4', '#222b31', '#d5ddd7', '#275774'),
    palette('Aged Parchment', 'Sepia and rust', '#f5ecd8', '#e8dbc3', '#fff8e8', '#33271e', '#e0d0b7', '#8a3e2a'),
  ],
}

/** Create a full palette from one color. The chosen hue is kept, while its
 * brightness is adjusted for contrast; users can still edit exact colors later. */
export function paletteFromColor(seed: string, light: boolean, terminal = false): ThemeColors {
  if (!/^#[\da-f]{6}$/i.test(seed)) seed = '#8b7cff'
  const channels = [1, 3, 5].map(i => parseInt(seed.slice(i, i + 2), 16))
  const blend = (target: number, amount: number): string => `#${channels.map(c => Math.round(c * amount + target * (1 - amount)).toString(16).padStart(2, '0')).join('')}`
  // Neutral foundations keep the text readable even for very bright or dark seeds.
  const background = blend(light ? 250 : 20, .08)
  const sidebar = blend(light ? 236 : 29, .12)
  const surface = blend(light ? 253 : 37, .10)
  const bubble = blend(light ? 222 : 47, .16)
  const text = light ? '#181a1f' : '#f5f5fa'
  const foreground = accessibleAccent(seed, [background, sidebar, surface, bubble])
  if (terminal) return { background, sidebar, surface, text: foreground }
  return { background, sidebar, surface, text, bubble, accent: foreground }
}

export function samePalette(a: ThemeColors, b: ThemeColors): boolean {
  const keys = Object.keys(a)
  return keys.length === Object.keys(b).length && keys.every(key => a[key as keyof ThemeColors]?.toLowerCase() === b[key as keyof ThemeColors]?.toLowerCase())
}
