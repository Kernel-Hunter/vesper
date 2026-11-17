import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { api, type AgentMode, type FallbackEntry, type FallbackState, type McpState, type Permissions, type Provider, type Thinking } from '../connection/api'
import {
  canPickFolder, closeSettings, getState, openFolder, pickFolder, saveSettings, setAgentMode, setAppearance, setSettingsTab,
  useStore, type SettingsTab,
} from '../store/store'
import { ACCENTS, EFFECTS, contrastRatio, withThemeColor, type ColorField, type EffectChoice, type Phosphor, type ThemeChoice, type ThemeColors, type ThemeStyle } from '../ui-theme/appearance'
import { THEME_PALETTES, paletteFromColor, samePalette } from '../ui-theme/palettes'
import { Icon, type IconName } from '../ui-primitives/Icon'

const TABS: { id: SettingsTab; label: string; icon: IconName }[] = [
  { id: 'models', label: 'Models and keys', icon: 'key' },
  { id: 'agent', label: 'Agent', icon: 'shield' },
  { id: 'tools', label: 'Tools (MCP)', icon: 'plug' },
  { id: 'appearance', label: 'Appearance', icon: 'palette' },
]

function Section({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return (
    <section className="set-section">
      <h3>{title}</h3>
      {desc && <p className="set-desc">{desc}</p>}
      {children}
    </section>
  )
}

// ---------------- Agent ----------------
const AGENT_MODES: { id: AgentMode; label: string; hint: string }[] = [
  { id: 'ask', label: 'Ask first', hint: 'Reads freely. Asks before every edit and every command.' },
  { id: 'auto-edit', label: 'Auto-edit', hint: 'Edits files without asking. Still asks before running commands.' },
  { id: 'yolo', label: 'Full access', hint: 'Edits and runs commands without asking. Only when you can afford to lose the files it can reach.' },
  { id: 'plan', label: 'Plan', hint: 'Reads and proposes a plan. Changes nothing until you approve the plan.' },
]
const THINKING: Thinking[] = ['off', 'low', 'medium', 'high']

/** Commands and web sites the user answered "always allow" to. */
function Rules() {
  const [perms, setPerms] = useState<Permissions | null>(null)
  useEffect(() => { api.permissions().then(setPerms, () => undefined) }, [])
  if (!perms) return null
  const rows = [...perms.commands.map(rule => ({ kind: 'commands' as const, rule })), ...perms.hosts.map(rule => ({ kind: 'hosts' as const, rule }))]
  return (
    <Section title="Runs without asking" desc={`Rules you saved with "Always allow". Read-only commands such as ${perms.safe.slice(0, 4).join(', ')} never ask.`}>
      {rows.length === 0 ? <p className="set-desc">No saved rules yet.</p> : (
        <div className="rule-list">
          {rows.map(r => (
            <div key={`${r.kind}:${r.rule}`} className="rule-row">
              <code>{r.rule}</code>
              <span className="dim">{r.kind === 'hosts' ? 'web site' : r.rule.startsWith('mcp__') ? 'MCP tool' : 'command'}</span>
              <button type="button" className="icon-btn sm" aria-label={`Remove the rule ${r.rule}`} title="Remove"
                onClick={() => { api.removePermission(r.kind, r.rule).then(setPerms, () => undefined) }}>
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </Section>
  )
}

// ---------------- Tools (MCP) ----------------
const MCP_EXAMPLE = `{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "\${GITHUB_TOKEN}" }
    }
  }
}`

const STATUS_LABEL: Record<string, string> = {
  ready: 'Ready', starting: 'Starting…', failed: 'Did not start', stopped: 'Stopped',
  'needs approval': 'Needs your approval', 'not started': 'Starts with the next message',
}

function ToolsTab() {
  const [state, setState] = useState<McpState | null>(null)
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const load = (s: McpState): void => { setState(s); setText(s.config || MCP_EXAMPLE) }
  useEffect(() => { api.mcp().then(load, e => setError(String((e as Error).message))) }, [])
  useEffect(() => { // a starting server turns ready on its own
    if (!state?.servers.some(s => s.status === 'starting')) return
    const t = window.setTimeout(() => { api.mcp().then(setState, () => undefined) }, 1000)
    return () => window.clearTimeout(t)
  }, [state])
  const run = async (action: 'approve' | 'restart' | 'config', body: { name?: string; text?: string } = {}): Promise<void> => {
    setBusy(true)
    setError('')
    try { load(await api.mcpAction(action, body)) } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  if (!state) return error ? <p className="set-desc">{error}</p> : null
  return (
    <>
      <Section title="Servers" desc="MCP servers give the agent more tools: GitHub, databases, browsers, docs and many more. Tools that change things ask first, like commands.">
        {state.servers.length === 0 ? <p className="set-desc">No servers yet. Add one below, or put a .mcp.json file in a project (the format other harnesses use works as is).</p> : (
          <div className="mcp-list">
            {state.servers.map(s => (
              <div key={`${s.scope}:${s.name}`} className={`mcp-row ${s.status.replace(' ', '-')}`}>
                <button type="button" className="mcp-main" onClick={() => setOpen(open === s.name ? null : s.name)} aria-expanded={open === s.name}>
                  <span className={`dot ${s.status === 'ready' ? 'ok' : s.status === 'failed' ? 'bad' : 'off'}`} />
                  <strong>{s.name}</strong>
                  <span className="dim">{s.scope === 'project' ? 'this project' : 'all projects'} · {s.transport}</span>
                  <span className="mcp-status">{STATUS_LABEL[s.status] ?? s.status}{s.status === 'ready' ? ` · ${s.tools.length} tools` : ''}</span>
                </button>
                {s.status === 'needs approval' && <button type="button" className="btn primary sm" disabled={busy} onClick={() => void run('approve', { name: s.name })}>Approve</button>}
                {(s.status === 'failed' || s.status === 'ready') && <button type="button" className="btn sm" disabled={busy} onClick={() => void run('restart', { name: s.name })}>Restart</button>}
                {open === s.name && (
                  <div className="mcp-detail">
                    {s.error && <p className="mcp-error">{s.error}</p>}
                    {s.status === 'needs approval' && <p className="set-desc">This server comes from {s.file}. Approving it lets that file start a program on this computer; approve only projects you trust.</p>}
                    {s.tools.length > 0 && <div className="mcp-tools">{s.tools.map(t => <code key={t}>{t}</code>)}</div>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {state.problems.map(p => <p key={p} className="mcp-error">{p}</p>)}
      </Section>
      <Section title="Your servers" desc={`Saved in ${state.config_path}, for every project. \${NAME} reads a key from your environment or ~/.vesper/.env, so secrets stay out of the file.`}>
        <textarea className="mcp-config" spellCheck={false} value={text} onChange={e => setText(e.target.value)} rows={12} aria-label="MCP servers (JSON)" />
        <div className="mcp-actions">
          {error && <span className="mcp-error">{error}</span>}
          <button type="button" className="btn" disabled={busy || text === (state.config || MCP_EXAMPLE)} onClick={() => setText(state.config || MCP_EXAMPLE)}>Reset</button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void run('config', { text })}>Save and start</button>
        </div>
      </Section>
    </>
  )
}

function AgentTab() {
  const server = useStore(s => s.server)
  const [path, setPath] = useState(server?.settings.workspace ?? '')
  const saved = server?.settings.workspace ?? ''
  useEffect(() => setPath(saved), [saved])
  if (!server) return null
  const s = server.settings
  return (
    <>
      <Section title="Working folder" desc={`Where Vesper starts: relative paths and commands run here. It can go to other folders when you ask. With none set it starts in your home folder (${server.home}).`}>
        <form className="key-input" onSubmit={e => { e.preventDefault(); if (path.trim()) void openFolder(path.trim()) }}>
          <input className="input mono" value={path} onChange={e => setPath(e.target.value)} placeholder="/Users/you/projects/my-app" aria-label="Folder path" spellCheck={false} />
          {canPickFolder() && <button type="button" className="btn ghost" onClick={() => void pickFolder()}><Icon name="folder" size={14} /> Browse</button>}
          <button type="submit" className="btn primary" disabled={!path.trim() || path.trim() === saved}>Open</button>
        </form>
      </Section>
      <Section title="Permissions" desc="What the agent may do on its own. You can change this at any time, even mid-chat.">
        <div className="mode-list">
          {AGENT_MODES.map(m => (
            <button type="button" key={m.id} className={`mode-card${s.agent_mode === m.id ? ' on' : ''}${m.id === 'yolo' ? ' risky' : ''}`}
              aria-pressed={s.agent_mode === m.id} onClick={() => void setAgentMode(m.id)}>
              <Icon name={m.id === 'yolo' ? 'warn' : m.id === 'plan' ? 'list' : 'shield'} size={15} />
              <span><strong>{m.label}</strong><small>{m.hint}</small></span>
            </button>
          ))}
        </div>
      </Section>
      <Rules />
      <Section title="Thinking" desc="How hard reasoning models think before they answer. Off lets the model decide. Higher levels are slower and use more tokens; the model must support reasoning.">
        <div className="segmented" role="group" aria-label="Thinking">
          {THINKING.map(level => (
            <button type="button" key={level} className={s.thinking === level ? 'on' : ''} aria-pressed={s.thinking === level}
              onClick={() => void saveSettings({ thinking: level })}>
              {level[0]!.toUpperCase() + level.slice(1)}
            </button>
          ))}
        </div>
      </Section>
    </>
  )
}

// ---------------- Models ----------------
function KeyRow({ p, focus }: { p: Provider; focus: boolean }) {
  const [value, setValue] = useState('')
  const [show, setShow] = useState(false)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (focus) { ref.current?.scrollIntoView({ block: 'center' }); ref.current?.focus() }
  }, [focus])

  const save = async (): Promise<void> => {
    const key = value.trim()
    if (!key) return
    if (await saveSettings({ keys: { [p.id]: key } }, `${p.label} key saved`)) setValue('')
  }
  return (
    <div className={`key-row${focus ? ' focus' : ''}`}>
      <div className="key-head">
        <span className={`dot ${p.has_key ? 'ok' : 'off'}`} />
        <strong>{p.label}</strong>
        {p.has_key && <span className="badge ok">Saved</span>}
        {p.key_url && (
          <a className="key-link" href={p.key_url} target="_blank" rel="noopener noreferrer">
            Get a key <Icon name="external" size={12} />
          </a>
        )}
      </div>
      <form className="key-input" onSubmit={e => { e.preventDefault(); void save() }}>
        <input
          ref={ref} className="input" type={show ? 'text' : 'password'} value={value}
          onChange={e => setValue(e.target.value)} autoComplete="off" spellCheck={false}
          placeholder={p.has_key ? '•••••••••••• (saved, paste to replace)' : 'Paste API key'}
          aria-label={`${p.label} API key`}
        />
        <button type="button" className="icon-btn sm" onClick={() => setShow(s => !s)} aria-label={show ? 'Hide key' : 'Show key'}>
          <Icon name={show ? 'eyeOff' : 'eye'} size={14} />
        </button>
        <button type="submit" className="btn primary" disabled={!value.trim()}>Save</button>
        {p.has_key && (
          <button type="button" className="btn ghost danger" onClick={() => void saveSettings({ keys: { [p.id]: '' } }, `${p.label} key removed`)}>
            Remove
          </button>
        )}
      </form>
    </div>
  )
}

function ModelsTab() {
  const server = useStore(s => s.server)
  const focus = useStore(s => s.settings.focusProvider)
  const [url, setUrl] = useState(server?.custom_base_url ?? '')
  const [smallProvider, setSmallProvider] = useState(server?.settings.small_provider ?? '')
  const [smallModel, setSmallModel] = useState(server?.settings.small_model ?? '')
  if (!server) return null
  const smallChanged = smallProvider !== server.settings.small_provider || smallModel.trim() !== server.settings.small_model
  const usable = server.providers.filter(p => p.local || p.has_key || p.id === smallProvider)
  const cloud = server.providers.filter(p => p.needs_key)
  const local = server.providers.filter(p => p.local)
  return (
    <>
      <Section title="API keys" desc="Keys stay on this computer, in ~/.vesper/.env, which only your user can read. The terminal app uses the same file: a key added in either place works in both.">
        <div className="key-list">
          {cloud.map(p => <KeyRow key={p.id} p={p} focus={focus === p.id} />)}
        </div>
      </Section>
      <Section title="Local and custom" desc={`Run models on your own machine with ${local.map(p => p.label).join(', ')}, or point Vesper at any OpenAI-compatible server.`}>
        <form className="key-input" onSubmit={e => { e.preventDefault(); void saveSettings({ custom_base_url: url.trim() }, 'Custom server saved') }}>
          <input className="input" value={url} onChange={e => setUrl(e.target.value)} placeholder="http://localhost:8000/v1" aria-label="Custom base URL" spellCheck={false} />
          <button type="submit" className="btn primary" disabled={url.trim() === server.custom_base_url}>Save</button>
        </form>
      </Section>
      <Section title="Small model" desc="A cheaper, faster model for chat titles and for summaries of long conversations. Leave it off and the main model does both.">
        <form className="key-input" onSubmit={e => {
          e.preventDefault()
          const model = smallProvider ? smallModel.trim() : ''
          void saveSettings({ small_provider: model ? smallProvider : '', small_model: model }, model ? 'Small model saved' : 'Small model off')
        }}>
          <select className="input" value={smallProvider} onChange={e => setSmallProvider(e.target.value)} aria-label="Small model provider">
            <option value="">Off</option>
            {usable.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <input className="input" value={smallModel} onChange={e => setSmallModel(e.target.value)} disabled={!smallProvider}
            placeholder="Model id, for example llama3.2" aria-label="Small model id" spellCheck={false} />
          <button type="submit" className="btn primary" disabled={!smallChanged || (!!smallProvider && !smallModel.trim())}>Save</button>
        </form>
      </Section>
      <FallbackSection />
    </>
  )
}

const WHY_LABEL: Record<string, string> = { 'your model': 'your model', paid: 'paid', free: 'free', local: 'on this computer', 'your order': '' }

function FallbackSection() {
  const [state, setState] = useState<FallbackState | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const load = (withOrder: boolean) => {
    setLoading(withOrder)
    api.fallback(withOrder).then(s => { setState(s); setError('') }, () => setError('Could not read the order.')).finally(() => setLoading(false))
  }
  useEffect(() => { load(true) }, [])
  const save = (body: { enabled?: boolean; chain?: FallbackEntry[] | null }) => {
    api.saveFallback(body).then(() => load(true), () => setError('Could not save the order.'))
  }
  const order = state?.order ?? []
  const keep = (list: FallbackEntry[]) => list.map(e => ({ provider: e.provider, model: e.model }))
  const move = (i: number, by: number) => {
    const next = [...order]
    const [item] = next.splice(i, 1)
    if (item) next.splice(i + by, 0, item)
    save({ chain: keep(next) })
  }
  return (
    <Section title="Automatic switching" desc="When a model runs out of credits, hits a rate limit or refuses its key, Vesper moves to the next model in this order and goes on with the same step. Paid models come first, then free ones from best to weakest, then a model on this computer. A model that failed is tried again later.">
      <label className="theme-art-toggle fallback-toggle">
        <input type="checkbox" checked={!!state?.enabled} disabled={!state} onChange={e => save({ enabled: e.target.checked })} />
        Switch models automatically
      </label>
      {error && <p className="set-desc">{error}</p>}
      {loading && !order.length ? <p className="set-desc">Reading the models your keys can use…</p> : order.length === 0 ? (
        <p className="set-desc">No other model found. Add an API key above, or start Ollama, to give Vesper somewhere to switch to.</p>
      ) : (
        <ol className="rule-list fallback-list" aria-label="Switching order">
          {order.map((e, i) => (
            <li key={`${e.provider}:${e.model}`} className="rule-row">
              <span className="fallback-n">{i + 1}</span>
              <code title={e.model}>{e.model}</code>
              <span className="dim">{e.label ?? e.provider}{e.why && WHY_LABEL[e.why] ? ` · ${WHY_LABEL[e.why]}` : ''}</span>
              <button type="button" className="icon-btn sm" aria-label={`Move ${e.model} up`} title="Move up" disabled={i === 0} onClick={() => move(i, -1)}>
                <Icon name="up" size={14} />
              </button>
              <button type="button" className="icon-btn sm flip" aria-label={`Move ${e.model} down`} title="Move down" disabled={i === order.length - 1} onClick={() => move(i, 1)}>
                <Icon name="up" size={14} />
              </button>
              <button type="button" className="icon-btn sm" aria-label={`Remove ${e.model} from the order`} title="Remove"
                onClick={() => save({ chain: keep(order.filter((_, j) => j !== i)) })}>
                <Icon name="trash" size={14} />
              </button>
            </li>
          ))}
        </ol>
      )}
      {state?.chain && (
        <button type="button" className="btn ghost fallback-reset" onClick={() => save({ chain: null })}>Reset to automatic order</button>
      )}
    </Section>
  )
}

// ---------------- Appearance ----------------
type ThemeCard = { id: ThemeChoice; label: string; mood: string; bg: string; side: string; ink: string; accent: string }
const THEME_GROUPS: { label: string; themes: ThemeCard[] }[] = [
  { label: 'The essentials', themes: [
    { id: 'system', label: 'System', mood: 'Follows your device', bg: '#212121', side: '#f8f4f1', ink: '#92908d', accent: '#ff7a45' },
    { id: 'dark', label: 'Dark', mood: 'Soft charcoal', bg: '#212121', side: '#303030', ink: '#92908d', accent: '#ff7a45' },
    { id: 'light', label: 'Light', mood: 'Clean and warm', bg: '#fffdfb', side: '#f8f4f1', ink: '#b7aaa2', accent: '#a83b16' },
  ] },
  { label: 'Studio', themes: [
    { id: 'dusk', label: 'Dusk', mood: 'Vesper’s own: indigo and starlight', bg: '#15141d', side: '#1d1b28', ink: '#a39cb6', accent: '#f2c46d' },
    { id: 'graphite', label: 'Graphite', mood: 'Neutral, no tint', bg: '#18191b', side: '#202124', ink: '#9a9fa8', accent: '#c7d0dc' },
    { id: 'carbon', label: 'Carbon', mood: 'True black for OLED', bg: '#000000', side: '#0b0b0c', ink: '#8f8f96', accent: '#ffffff' },
  ] },
  { label: 'After dark', themes: [
    { id: 'midnight', label: 'Midnight', mood: 'Indigo skies', bg: '#101525', side: '#171f35', ink: '#7183a7', accent: '#b3bfff' },
    { id: 'ocean', label: 'Ocean', mood: 'Deep sea glass', bg: '#0b2028', side: '#10303a', ink: '#648f94', accent: '#73e0d0' },
    { id: 'forest', label: 'Forest', mood: 'Moss and fern', bg: '#111f19', side: '#1a2d22', ink: '#729279', accent: '#a4db9d' },
    { id: 'nebula', label: 'Nebula', mood: 'Plum stardust', bg: '#20172e', side: '#2b203b', ink: '#9c82ad', accent: '#d0adff' },
    { id: 'ember', label: 'Ember', mood: 'Fireside glow', bg: '#271a17', side: '#37251f', ink: '#a48370', accent: '#ffb487' },
    { id: 'rose', label: 'Rosewood', mood: 'Velvet dusk', bg: '#291b24', side: '#3a2733', ink: '#ac879a', accent: '#ffb2d3' },
  ] },
  { label: 'Daylight', themes: [
    { id: 'sand', label: 'Sandstone', mood: 'Sunlit paper', bg: '#faf5eb', side: '#f0e6d6', ink: '#b7a48c', accent: '#9b481d' },
    { id: 'lavender', label: 'Lavender', mood: 'Quiet lilac', bg: '#f8f5fc', side: '#eee8f6', ink: '#b3a3c1', accent: '#7043a0' },
    { id: 'sky', label: 'Sky', mood: 'Cool morning', bg: '#f2f8fc', side: '#e2f0f7', ink: '#92b4c4', accent: '#1d628e' },
  ] },
  { label: 'Retro', themes: [
    { id: 'terminal', label: 'Terminal', mood: 'Phosphor console', bg: '#030604', side: '#0b1b0e', ink: '#39ff7a', accent: '#39ff7a' },
    { id: 'workbench', label: 'Workbench', mood: 'Classic desktop', bg: '#e8e5d8', side: '#d5d1c2', ink: '#676b60', accent: '#173f86' },
    { id: 'synthwave', label: 'Synthwave', mood: 'Neon after dark', bg: '#180d2c', side: '#201238', ink: '#aa86b9', accent: '#ff9dde' },
    { id: 'pixel', label: 'Pixel Arcade', mood: '8-bit command deck', bg: '#11172c', side: '#1a2340', ink: '#91a8c9', accent: '#85e8f5' },
    { id: 'typewriter', label: 'Typewriter', mood: 'Paper and ink', bg: '#f5edda', side: '#e8ddc5', ink: '#a18b6b', accent: '#873c28' },
  ] },
]
const PHOSPHORS: { id: Phosphor; label: string; color: string }[] = [
  { id: 'green', label: 'Green', color: '#39ff7a' }, { id: 'amber', label: 'Amber', color: '#ffb000' },
  { id: 'cyan', label: 'Cyan', color: '#3df2ff' }, { id: 'white', label: 'White', color: '#e8f0ff' }, { id: 'red', label: 'Red', color: '#ff3b5c' },
  { id: 'violet', label: 'Violet', color: '#c69bff' }, { id: 'pink', label: 'Pink', color: '#ff80ca' }, { id: 'blue', label: 'Blue', color: '#7bb8ff' },
]

function ThemePreview({ theme }: { theme: ThemeCard }) {
  const style = { '--p-bg': theme.bg, '--p-side': theme.side, '--p-ink': theme.ink, '--p-accent': theme.accent } as CSSProperties
  return (
    <div className={`theme-preview tp-${theme.id}`} style={style} aria-hidden="true">
      <div className="tp-side"><i /><i /><i /></div>
      <div className="tp-main">
        <div className="tp-top"><b /><em /></div>
        {theme.id === 'terminal' ? <div className="tp-terminal-text">$ make<br />&gt; ready_</div> : <div className="tp-lines"><b /><b className="tp-short" /></div>}
        <div className="tp-compose"><b /><em /></div>
      </div>
    </div>
  )
}

const COLOR_CONTROLS: { field: ColorField; label: string; token: string; fallback: string; hint: string }[] = [
  { field: 'background', label: 'Canvas', token: '--bg', fallback: '#212121', hint: 'Behind the conversation' },
  { field: 'sidebar', label: 'Sidebar', token: '--bg-side', fallback: '#303030', hint: 'Navigation and panels' },
  { field: 'surface', label: 'Surfaces', token: '--layer-1', fallback: '#2c2c2c', hint: 'Composer and cards' },
  { field: 'text', label: 'Text', token: '--text', fallback: '#f2f1ef', hint: 'Primary interface text' },
  { field: 'bubble', label: 'Your messages', token: '--bubble', fallback: '#383838', hint: 'Sent message background' },
  { field: 'accent', label: 'Accent', token: '--accent', fallback: '#ff7a45', hint: 'Buttons and highlights' },
]
const COLOR_HEX = /^#[\da-f]{6}$/i

function displayedColor(token: string, fallback: string): string {
  const css = getComputedStyle(document.documentElement)
  const value = css.getPropertyValue(token).trim()
  if (COLOR_HEX.test(value)) return value.toLowerCase()
  // Terminal surfaces are translucent phosphor over the canvas; show their actual color.
  const rgba = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)$/i.exec(value)
  const bg = css.getPropertyValue('--bg').trim()
  if (!rgba || !COLOR_HEX.test(bg)) return fallback
  const alpha = rgba[4] === undefined ? 1 : Number(rgba[4])
  const channels = [1, 2, 3].map((n, i) => Math.round(Number(rgba[n]) * alpha + parseInt(bg.slice(1 + i * 2, 3 + i * 2), 16) * (1 - alpha)))
  return `#${channels.map(n => n.toString(16).padStart(2, '0')).join('')}`
}

function ColorControl({ label, hint, value, modified, onChange, onReset }: {
  label: string; hint: string; value: string; modified: boolean; onChange: (color: string) => void; onReset: () => void
}) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  const invalid = !COLOR_HEX.test(draft)
  return <div className="color-control">
    <div className="color-control-name"><strong>{label}</strong><small>{hint}</small></div>
    <label className="color-well" title={`Choose ${label.toLowerCase()} color`}>
      <input type="color" aria-label={`${label} color`} value={value} onChange={e => onChange(e.target.value)} />
    </label>
    <input className="input color-hex" aria-label={`${label} hex color`} aria-invalid={invalid || undefined}
      value={draft} maxLength={7} spellCheck={false} onChange={e => { const next = e.target.value; setDraft(next); if (COLOR_HEX.test(next)) onChange(next) }}
      onBlur={() => { if (invalid) setDraft(value) }} />
    <button type="button" className="color-reset" disabled={!modified} onClick={onReset} aria-label={`Reset ${label.toLowerCase()} color`}>Reset</button>
  </div>
}

function Choice<T extends string>({ label, value, options, onChange, note }: {
  label: string; value: T; options: [T, string][]; onChange: (value: T) => void; note?: string
}) {
  return (
    <div className="layout-choice">
      <span className="layout-label">{label}</span>
      <div className="segmented" role="group" aria-label={label}>
        {options.map(([id, text]) => <button type="button" key={id} className={value === id ? 'on' : ''} aria-pressed={value === id} onClick={() => onChange(id)}>{text}</button>)}
      </div>
      {note && <small className="layout-note">{note}</small>}
    </div>
  )
}

function AppearanceTab() {
  const a = useStore(s => s.appearance)
  const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  const colors = a.palettes[a.theme] ?? {}
  const style = a.styles[a.theme] ?? {}
  const setStyle = (patch: Partial<ThemeStyle>): void => {
    const next = { ...style, ...patch }
    for (const key of Object.keys(next) as (keyof ThemeStyle)[]) if (next[key] === undefined) delete next[key]
    const styles = { ...a.styles }
    if (Object.keys(next).length) styles[a.theme] = next
    else delete styles[a.theme]
    setAppearance({ styles })
  }
  const terminal = a.theme === 'terminal'
  const controls = COLOR_CONTROLS.filter(c => !terminal || (c.field !== 'bubble' && c.field !== 'accent'))
  const current = Object.fromEntries(controls.map(c => [c.field, colors[c.field] ?? displayedColor(c.token, c.fallback)])) as Record<ColorField, string>
  const backgrounds = [current.background, current.sidebar, current.surface, ...(terminal ? [] : [current.bubble])]
  const poorText = backgrounds.some(bg => contrastRatio(current.text, bg) < 4.5)
  const poorAccent = !terminal && backgrounds.slice(0, 3).some(bg => contrastRatio(current.accent, bg) < 4.5)
  const withoutSeed = (): typeof a.paletteSeeds => { const seeds = { ...a.paletteSeeds }; delete seeds[a.theme]; return seeds }
  const setColor = (field: ColorField, color: string | null): void => {
    setAppearance({ palettes: withThemeColor(a.palettes, a.theme, field, color), paletteSeeds: withoutSeed(),
      ...(field === 'accent' ? { accent: 'theme' as const } : {}) })
  }
  const selectedAccent = colors.accent ? (ACCENTS.find(p => p.color?.toLowerCase() === colors.accent)?.id ?? 'custom') : a.accent
  const themeCard = THEME_GROUPS.flatMap(group => group.themes).find(t => t.id === a.theme)!
  const original = !Object.keys(colors).length && a.accent === 'theme'
  const selectedPalette = THEME_PALETTES[a.theme].find(p => samePalette(colors, p.colors))
  const light = contrastRatio(current.background, '#000000') > contrastRatio(current.background, '#ffffff')
  const seed = a.paletteSeeds[a.theme] ?? (terminal ? current.text : current.accent)
  const applyPalette = (palette: ThemeColors | null, source?: string): void => {
    const palettes = { ...a.palettes }
    if (palette) palettes[a.theme] = { ...palette }
    else delete palettes[a.theme]
    const paletteSeeds = withoutSeed()
    if (source) paletteSeeds[a.theme] = source.toLowerCase()
    setAppearance({ palettes, paletteSeeds, accent: 'theme', ...(!palette && terminal ? { phosphor: 'green' as const } : {}) })
  }
  return (
    <>
      <Section title="Make it yours" desc="Each theme has its own interface and matching accent. Changes apply instantly and are saved on this device.">
        {THEME_GROUPS.map(group => <div className="theme-group" key={group.label}>
          <h4>{group.label}</h4>
          <div className="theme-grid">
            {group.themes.map(t => (
              <button type="button" key={t.id} className={`theme-card${a.theme === t.id ? ' on' : ''}`} onClick={() => setAppearance({ theme: t.id, accent: 'theme' })} aria-pressed={a.theme === t.id}>
                <ThemePreview theme={t} />
                <span className="theme-label"><strong>{t.id === 'terminal' && <Icon name="terminal" size={13} />}{t.label}</strong><small>{t.mood}</small></span>
              </button>
            ))}
          </div>
        </div>)}
      </Section>
      <Section title="Text and layout" desc="How big the text is, how much room things get, and how wide the conversation runs. Applies to every theme.">
        <div className="layout-options">
          <Choice label="Text size" value={a.textSize} onChange={v => setAppearance({ textSize: v })}
            options={[['sm', 'Small'], ['md', 'Default'], ['lg', 'Large'], ['xl', 'Larger']]} />
          <Choice label="Density" value={a.density} onChange={v => setAppearance({ density: v })}
            options={[['compact', 'Compact'], ['comfortable', 'Comfortable'], ['spacious', 'Spacious']]} />
          <Choice label="Conversation width" value={a.width} onChange={v => setAppearance({ width: v })}
            options={[['narrow', 'Narrow'], ['standard', 'Standard'], ['wide', 'Wide']]} />
          <Choice label="Your messages" value={a.userStyle} onChange={v => setAppearance({ userStyle: v })}
            options={[['bubble', 'Bubbles'], ['plain', 'Inline']]} />
          <Choice label="Interface font" value={a.font} onChange={v => setAppearance({ font: v })}
            note={style.font ? 'This theme sets its own type style below; that one wins.' : undefined}
            options={[['system', 'System'], ['rounded', 'Rounded'], ['humanist', 'Humanist'], ['serif', 'Serif'], ['mono', 'Mono']]} />
        </div>
      </Section>
      <Section title="Color palettes" desc={a.theme === 'system' ? 'Choose a complete look, or select Original to follow your device’s colors.' : 'Choose a complete look for this theme. Each palette updates all the colors together and is saved with the theme.'}>
        <div className="palette-grid">
          <button type="button" className={`palette-card${original ? ' on' : ''}`} aria-pressed={original} onClick={() => applyPalette(null)}>
            <span className="palette-sample" style={{ '--palette-bg': themeCard.bg, '--palette-side': themeCard.side, '--palette-surface': themeCard.bg, '--palette-text': themeCard.ink, '--palette-accent': themeCard.accent } as CSSProperties} aria-hidden="true"><i /><b /><em /></span>
            <strong>Original</strong><small>Theme default</small>
          </button>
          {THEME_PALETTES[a.theme].map(p => <button type="button" key={p.label} className={`palette-card${selectedPalette === p ? ' on' : ''}`} aria-pressed={selectedPalette === p} onClick={() => applyPalette(p.colors)}>
            <span className="palette-sample" style={{ '--palette-bg': p.colors.background, '--palette-side': p.colors.sidebar, '--palette-surface': p.colors.surface, '--palette-text': p.colors.text, '--palette-accent': p.colors.accent ?? p.colors.text } as CSSProperties} aria-hidden="true"><i /><b /><em /></span>
            <strong>{p.label}</strong><small>{p.note}</small>
          </button>)}
        </div>
        <p className="palette-current">Current: <strong>{original ? 'Original' : selectedPalette?.label ?? 'Custom palette'}</strong></p>
        <div className="palette-generator">
          <div><strong>Make it your color</strong><small>Pick one color and we’ll create a coordinated, readable palette.</small></div>
          <label className="color-well" title="Choose a palette color"><input type="color" aria-label="Palette color" value={seed} onChange={e => applyPalette(paletteFromColor(e.target.value, terminal ? false : light, terminal), e.target.value)} /></label>
          {!terminal && <div className="palette-tones" aria-label="Palette brightness">
            <button type="button" className={!light ? 'on' : ''} aria-pressed={!light} onClick={() => applyPalette(paletteFromColor(seed, false), seed)}>Dark</button>
            <button type="button" className={light ? 'on' : ''} aria-pressed={light} onClick={() => applyPalette(paletteFromColor(seed, true), seed)}>Light</button>
          </div>}
        </div>
        {(poorText || poorAccent) && <p className="color-warning" role="status">Some {poorText && poorAccent ? 'text and accents may' : poorText ? 'text may' : 'accents may'} be hard to read on these surfaces. Your chosen colors are kept exactly as entered.</p>}
      </Section>
      <details className="color-advanced">
        <summary>Fine-tune individual colors <span>Optional</span></summary>
      {!terminal && <Section title="Accent shortcuts" desc="Change only the accent, without replacing the whole palette.">
        <div className="swatches accent-swatches">
          {ACCENTS.map(p => <button type="button" key={p.id} className={`swatch${selectedAccent === p.id ? ' on' : ''}`}
            onClick={() => setAppearance({ accent: p.id, palettes: withThemeColor(a.palettes, a.theme, 'accent', p.color ?? null), paletteSeeds: withoutSeed() })} aria-pressed={selectedAccent === p.id}>
            <span className={p.id === 'theme' ? 'swatch-default' : ''} style={p.color ? { background: p.color } : undefined} />{p.label}
          </button>)}
          <label className={`swatch custom-swatch${selectedAccent === 'custom' ? ' on' : ''}`}>
            <input type="color" aria-label="Custom accent color" value={colors.accent ?? a.customAccent}
              onChange={e => setAppearance({ accent: 'custom', customAccent: e.target.value, palettes: withThemeColor(a.palettes, a.theme, 'accent', e.target.value), paletteSeeds: withoutSeed() })} />
            Custom
          </label>
        </div>
      </Section>}
      {a.theme === 'terminal' && (
        <Section title="Phosphor" desc="Color of the terminal glow.">
          <div className="swatches">
            {PHOSPHORS.map(p => (
              <button type="button" key={p.id} className={`swatch${!colors.text && a.phosphor === p.id ? ' on' : ''}`} onClick={() => setAppearance({ phosphor: p.id, palettes: withThemeColor(a.palettes, a.theme, 'text', null), paletteSeeds: withoutSeed() })} aria-pressed={!colors.text && a.phosphor === p.id} title={p.label}>
                <span style={{ background: p.color, boxShadow: `0 0 12px ${p.color}` }} />{p.label}
              </button>
            ))}
          </div>
        </Section>
      )}
      <Section title="Color studio" desc="Set exact colors for this theme. Your edits stay with it when you switch themes. Enter a hex code or use the color picker.">
        <div className="color-studio">
          {controls.map(c => <ColorControl key={c.field} label={terminal && c.field === 'text' ? 'Phosphor' : c.label}
            hint={terminal && c.field === 'text' ? 'Terminal text and glow' : c.hint} value={current[c.field]} modified={!!colors[c.field]}
            onChange={color => setColor(c.field, color)} onReset={() => setColor(c.field, null)} />)}
        </div>
        <button type="button" className="btn ghost color-reset-all" disabled={!Object.keys(colors).length && a.accent === 'theme'}
          onClick={() => applyPalette(null)}>
          Reset this theme’s colors
        </button>
      </Section>
      </details>
      <Section title="Theme details" desc="Fine-tune this theme without changing your other themes.">
        <div className="theme-details">
          <label>Type style<select className="input" value={style.font ?? 'theme'} onChange={e => setStyle({ font: e.target.value === 'theme' ? undefined : e.target.value as ThemeStyle['font'] })}>
            <option value="theme">Theme default</option><option value="sans">Sans serif</option><option value="serif">Serif</option><option value="mono">Monospace</option>
          </select></label>
          <label>Corner style<select className="input" value={style.shape ?? 'theme'} onChange={e => setStyle({ shape: e.target.value === 'theme' ? undefined : e.target.value as ThemeStyle['shape'] })}>
            <option value="theme">Theme default</option><option value="square">Square</option><option value="rounded">Rounded</option>
          </select></label>
          <label className="theme-art-toggle"><input type="checkbox" checked={style.art !== false} onChange={e => setStyle({ art: e.target.checked ? undefined : false })} /> Show theme artwork and textures</label>
        </div>
        <button type="button" className="btn ghost color-reset-all" disabled={!Object.keys(style).length} onClick={() => { const styles = { ...a.styles }; delete styles[a.theme]; setAppearance({ styles }) }}>Reset theme details</button>
      </Section>
      <Section title="Background effect" desc={reduced ? 'Your system asks for reduced motion, so effects stay off.' : 'Runs behind the chat at low frame rate and pauses when the window is hidden.'}>
        <div className="effect-grid">
          {EFFECTS.map(e => (
            <button type="button" key={e.id} className={`effect-card${a.effect === e.id ? ' on' : ''}`} onClick={() => setAppearance({ effect: e.id as EffectChoice })} title={e.hint} aria-pressed={a.effect === e.id}>
              <span className={`fx-thumb fx-${e.id}`} aria-hidden="true" />
              {e.label}
            </button>
          ))}
        </div>
        {a.effect !== 'none' && (
          <label className="slider">
            <div className="slider-head"><span>Intensity</span><output>{a.intensity}%</output></div>
            <input type="range" min={10} max={100} step={5} value={a.intensity} onChange={e => setAppearance({ intensity: Number(e.target.value) })} />
          </label>
        )}
      </Section>
    </>
  )
}

// ---------------- Dialog ----------------
export function Settings() {
  const { open, tab } = useStore(s => s.settings)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const prev = document.activeElement as HTMLElement | null
    if (!getState().settings.focusProvider) ref.current?.querySelector<HTMLElement>('.set-tab.on')?.focus()
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') { e.preventDefault(); closeSettings() }
      if (e.key === 'Tab' && ref.current) { // keep focus inside the dialog
        const items = [...ref.current.querySelectorAll<HTMLElement>('button, input, select, a[href], [tabindex="0"]')].filter(el => !el.hasAttribute('disabled') && el.offsetParent !== null)
        const first = items[0]; const last = items[items.length - 1]
        if (!first || !last) return
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); prev?.focus?.() }
  }, [open])

  if (!open) return null
  return (
    <div className="scrim" onPointerDown={e => { if (e.target === e.currentTarget) closeSettings() }}>
      <div className="settings" role="dialog" aria-modal="true" aria-label="Settings" ref={ref}>
        <nav className="set-nav">
          <h2>Settings</h2>
          {TABS.map(t => (
            <button type="button" key={t.id} className={`set-tab${tab === t.id ? ' on' : ''}`} onClick={() => setSettingsTab(t.id)} aria-current={tab === t.id ? 'page' : undefined}>
              <Icon name={t.icon} size={16} />{t.label}
            </button>
          ))}
        </nav>
        <div className="set-body">
          <header className="set-head">
            <h2>{TABS.find(t => t.id === tab)?.label}</h2>
            <div className="set-head-actions">
              {tab === 'appearance' && <button type="button" className="btn ghost" onClick={closeSettings}>View in app</button>}
              <button type="button" className="icon-btn" onClick={closeSettings} aria-label="Close settings"><Icon name="close" size={16} /></button>
            </div>
          </header>
          <div className="set-scroll">
            {tab === 'agent' && <AgentTab />}
            {tab === 'models' && <ModelsTab />}
            {tab === 'tools' && <ToolsTab />}
            {tab === 'appearance' && <AppearanceTab />}
          </div>
        </div>
      </div>
    </div>
  )
}
