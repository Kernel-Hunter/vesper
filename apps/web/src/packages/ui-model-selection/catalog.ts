/** What the engine knows about each model a provider serves (see providers.model_catalog). */
export interface ModelInfo {
  id: string
  name: string
  context: number | null
  /** Tool calling: true, false, or null when the provider does not say. */
  tools: boolean | null
  /** US dollars per million tokens; null when unknown. */
  input: number | null
  output: number | null
  free: boolean | null
  /** provider: the provider's own list; openrouter: the maker's list price; local: runs on this computer. */
  price_source: 'provider' | 'openrouter' | 'local' | null
}

export interface Catalog {
  models: ModelInfo[]
  /** False when the list could not be fetched; `error` says why and `models` is a fallback. */
  live: boolean
  error: string | null
  fetched_at: number | null
}

export type ModelFilter = 'all' | 'free' | 'tools'

const money = (n: number): string => (n === 0 ? '$0' : `$${Number(n.toPrecision(n < 1 ? 2 : 3))}`)

/** "Free", "$0.15 / $0.6" (input / output per million tokens), or "" when the price is unknown. */
export function formatPrice(m: ModelInfo): string {
  if (m.free) return m.price_source === 'local' ? 'Local' : 'Free'
  if (m.input === null || m.output === null) return ''
  return `${money(m.input)} / ${money(m.output)}`
}

export function priceTitle(m: ModelInfo): string {
  if (m.price_source === 'local') return 'Runs on this computer: no cost per token'
  if (m.free) return 'Free to use (the provider may limit how often)'
  if (m.input === null || m.output === null) return 'The provider does not publish a price for this model in its list'
  const source = m.price_source === 'openrouter' ? " (the maker's list price, as listed on OpenRouter)" : ''
  return `${money(m.input)} per million input tokens, ${money(m.output)} per million output tokens${source}`
}

/** 128000 -> "128k", 1048576 -> "1M". */
export function formatContext(tokens: number | null): string {
  if (!tokens || tokens <= 0) return ''
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(1))}M`
  return `${Math.round(tokens / 1000)}k`
}

/** Every word of the query must appear in the id or the name. */
export function filterModels(models: ModelInfo[], query: string, filter: ModelFilter): ModelInfo[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return models.filter(m => {
    if (filter === 'free' && m.free !== true) return false
    if (filter === 'tools' && m.tools === false) return false
    const text = `${m.id} ${m.name}`.toLowerCase()
    return words.every(w => text.includes(w))
  })
}
