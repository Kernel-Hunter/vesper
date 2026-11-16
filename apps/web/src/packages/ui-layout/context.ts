/** Compact token counts without treating millions and billions as thousands. */
export function tokenCount(value: number): string {
  const n = Math.max(0, Number.isFinite(value) ? value : 0)
  for (const [size, suffix] of [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']] as const) {
    if (n >= size || (size > 1e3 && n >= size - size / 2000)) {
      const scaled = n / size
      return `${Number(scaled.toFixed(scaled < 10 ? 2 : scaled < 100 ? 1 : 0))}${suffix}`
    }
  }
  return String(Math.round(n))
}

export function percent(value: number, total: number): string {
  if (total <= 0) return '0%'
  const n = Math.max(0, value / total * 100)
  return `${n > 0 && n < 0.1 ? '<0.1' : Number(n.toFixed(n < 10 ? 1 : 0))}%`
}
