import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../connection/api'
import { openSettings, saveSettings, useStore } from '../store/store'
import { Icon } from '../ui-primitives/Icon'
import { type Catalog, filterModels, formatContext, formatPrice, type ModelFilter, priceTitle } from './catalog'
import { effortLabel, effortSupport } from './effort'

// Live model lists, kept for a minute: keys and models can change in the terminal app meanwhile.
// A list that could not be fetched is never kept, so the next open tries again.
const modelCache = new Map<string, { at: number; catalog: Catalog }>()
const fresh = (id: string): Catalog | null => {
  const hit = modelCache.get(id)
  return hit && Date.now() - hit.at < 60_000 ? hit.catalog : null
}

export function ModelPicker({ onClose }: { onClose: () => void }) {
  const server = useStore(s => s.server)
  const [active, setActive] = useState(server?.settings.provider ?? '')
  const [query, setQuery] = useState('')
  const [catalog, setCatalog] = useState<Catalog | null>(fresh(active))
  const [filter, setFilter] = useState<ModelFilter>('all')
  const [loading, setLoading] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const ref = useRef<HTMLDivElement>(null)
  const search = useRef<HTMLInputElement>(null)

  const provider = server?.providers.find(p => p.id === active)
  const locked = !!provider && provider.needs_key && !provider.has_key

  useEffect(() => { search.current?.focus() }, [active])
  useEffect(() => { ref.current?.querySelector('.picker-provider.active')?.scrollIntoView({ block: 'nearest' }) }, [])

  // Close on outside click / Escape.
  useEffect(() => {
    const onDown = (e: PointerEvent): void => {
      const el = ref.current
      const t = e.target as Node
      if (el && !el.contains(t) && !(t instanceof Element && t.closest('.chip-btn'))) onClose()
    }
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey, true)
    return () => { document.removeEventListener('pointerdown', onDown); document.removeEventListener('keydown', onKey, true) }
  }, [onClose])

  // Fetch the provider's model list: every model the key can use, with prices where known.
  useEffect(() => {
    if (!provider || locked) { setCatalog(null); return }
    const hit = attempt === 0 ? fresh(provider.id) : null
    if (hit) { setCatalog(hit); return }
    let alive = true
    setCatalog(null)
    setLoading(true)
    api.models(provider.id)
      .then(found => {
        if (found.live) modelCache.set(provider.id, { at: Date.now(), catalog: found })
        if (alive) setCatalog(found)
      })
      .catch((e: unknown) => {
        if (alive) setCatalog({ models: [], live: false, error: e instanceof Error ? e.message : 'The engine did not answer.', fetched_at: null })
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [provider?.id, locked, provider?.has_key, attempt]) // eslint-disable-line react-hooks/exhaustive-deps

  const all = catalog?.models ?? []
  const models = useMemo(() => filterModels(all, query, filter), [all, query, filter])
  const counts = useMemo(() => ({
    free: all.filter(m => m.free === true).length,
    noTools: all.filter(m => m.tools === false).length,
  }), [all])
  const ids = useMemo(() => new Set(all.map(m => m.id)), [all])

  if (!server) return null
  const current = server.settings

  const choose = async (model: string): Promise<void> => {
    if (!provider) return
    const ok = await saveSettings({ provider: provider.id, model }, `Using ${provider.label} · ${model}`)
    if (ok) onClose()
  }

  const custom = query.trim()
  const showCustom = !!custom && !ids.has(custom) && /^[\w.:/@+-]{1,200}$/.test(custom)

  return (
    <div className="picker" ref={ref} role="dialog" aria-label="Choose a model">
      <div className="picker-providers" role="listbox" aria-label="Providers">
        {server.providers.map(p => (
          <button
            type="button" key={p.id} role="option" aria-selected={p.id === active}
            className={`picker-provider${p.id === active ? ' active' : ''}`}
            onClick={() => { setActive(p.id); setQuery('') }}
          >
            <span className={`dot ${p.local ? 'local' : p.has_key || !p.needs_key ? 'ok' : 'off'}`} />
            <span className="pp-label">{p.label}</span>
            {p.id === current.provider && <Icon name="check" size={13} />}
          </button>
        ))}
      </div>
      <div className="picker-models">
        {locked ? (
          <div className="picker-locked">
            <Icon name="key" size={22} />
            <p>{provider?.label} needs an API key.</p>
            <button type="button" className="btn primary" onClick={() => { onClose(); openSettings('models', provider?.id ?? null) }}>
              Add key
            </button>
          </div>
        ) : (
          <>
            <div className="picker-search">
              <Icon name="search" size={14} />
              <input
                ref={search} value={query} onChange={e => setQuery(e.target.value)}
                placeholder="Search or type a model id" aria-label="Search models"
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    const pick = showCustom ? custom : models[0]?.id
                    if (pick) void choose(pick)
                  }
                }}
              />
              {loading && <span className="spinner" aria-label="Loading models" />}
            </div>
            <div className="picker-filters" role="group" aria-label="Show">
              <button type="button" className={filter === 'all' ? 'on' : ''} aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>
                All <span>{all.length}</span>
              </button>
              <button type="button" className={filter === 'free' ? 'on' : ''} aria-pressed={filter === 'free'} onClick={() => setFilter('free')}
                disabled={!counts.free} title={counts.free ? 'Models that cost nothing per token' : 'This provider lists no free models'}>
                Free <span>{counts.free}</span>
              </button>
              {counts.noTools > 0 && (
                <button type="button" className={filter === 'tools' ? 'on' : ''} aria-pressed={filter === 'tools'} onClick={() => setFilter('tools')}
                  title="Vesper needs tool calling to read files and run commands">
                  Works as agent <span>{all.length - counts.noTools}</span>
                </button>
              )}
              <span className="picker-unit" title="Price in US dollars per million input / output tokens">$ per 1M in / out</span>
            </div>
            {catalog && !catalog.live && catalog.error && (
              <div className="picker-note" role="status">
                <span>{catalog.error}{catalog.models.length ? (catalog.fetched_at ? ' Showing the last list fetched.' : ' Showing suggested models.') : ''}</span>
                <button type="button" onClick={() => setAttempt(a => a + 1)}>Retry</button>
              </div>
            )}
            <div className="picker-list">
              {showCustom && (
                <button type="button" className="picker-model" onClick={() => void choose(custom)}>
                  <Icon name="plus" size={13} /> Use “{custom}”
                </button>
              )}
              {models.map(m => {
                const on = m.id === current.model && active === current.provider
                const price = formatPrice(m)
                const effort = effortSupport(active, m.id)
                return (
                  <button type="button" key={m.id} className={`picker-model${on ? ' active' : ''}`} onClick={() => void choose(m.id)}
                    title={m.name && m.name !== m.id ? m.name : undefined}>
                    <span className="pm-id">{m.id}</span>
                    {m.tools === false && <small className="pm-warn" title="This model cannot call tools, so Vesper cannot read files or run commands with it">no tools</small>}
                    {effort === 'supported' && <small className="picker-effort supported" title={effortLabel[effort]}>effort</small>}
                    {formatContext(m.context) && <small className="pm-ctx" title={`Context window: ${m.context?.toLocaleString()} tokens`}>{formatContext(m.context)}</small>}
                    <small className={`pm-price${m.free ? ' free' : ''}${price ? '' : ' unknown'}`} title={priceTitle(m)}>{price || '—'}</small>
                    {on && <Icon name="check" size={13} />}
                  </button>
                )
              })}
              {!models.length && !showCustom && !loading && <p className="picker-empty">No models found. Type a model id.</p>}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
