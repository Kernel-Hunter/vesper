import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type SavedSession } from '../connection/api'
import {
  currentChat, deleteSession, folderName, forkSession, isDesktop, newChat, openFolderIn, openSession, openSessionWindow, openSettings,
  renameSession, selectChat, toggleTranscript, updateSidebar, useStore,
} from '../store/store'
import { Icon } from '../ui-primitives/Icon'
import { Wordmark } from '../ui-primitives/Logo'

export interface Row {
  id: string
  title: string
  updated: number
  cwd: string
  source: string
  saved: boolean
  working: boolean
  needsApproval: boolean
  pinned: boolean
  archived: boolean
  unread: boolean
  completed: boolean
  section: string
  position: number
}

function sectionName(row: Row): string { return row.section || (row.cwd ? folderName(row.cwd) : 'Other') }

function ChatRow({ row, active, onMenu }: { row: Row; active: boolean; onMenu: (row: Row, x: number, y: number) => void }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(row.title)
  const done = (): void => { setEditing(false); if (title.trim() && title.trim() !== row.title) void renameSession(row.id, title) }
  if (editing) return (
    <form className="chat-row editing" onSubmit={e => { e.preventDefault(); done() }}>
      <input className="input" value={title} autoFocus maxLength={80} aria-label="Conversation title"
        onChange={e => setTitle(e.target.value)} onBlur={done}
        onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setTitle(row.title); setEditing(false) } }} />
    </form>
  )
  return (
    <div className={`chat-row${active ? ' active' : ''}${row.unread ? ' unread' : ''}`}
      onContextMenu={e => { if (row.saved) { e.preventDefault(); onMenu(row, e.clientX, e.clientY) } }}>
      <button type="button" className="chat-title" title={`${row.title}${row.cwd ? `\n${row.cwd}` : ''}`}
        onClick={() => { if (row.saved) void openSession(row.id); else selectChat(row.id) }}
        onDoubleClick={() => { if (row.saved) { setTitle(row.title); setEditing(true) } }}>
        <Icon name={row.source === 'desktop' ? 'code' : 'terminal'} size={14} />
        <span className="saved-title">{row.completed && <span title="Completed">✓ </span>}{row.title || 'Untitled'}
          {row.needsApproval ? <span className="chat-attention" aria-label="Waiting for approval" title="Waiting for approval">!</span>
            : row.working ? <span className="chat-working" aria-label="Working" title="Working" />
              : row.unread ? <span className="chat-unread" aria-label="Unread" title="Unread" /> : null}</span>
      </button>
      {row.saved && <div className="chat-tools"><button type="button" className="icon-btn sm chat-menu-button" aria-label={`Options for ${row.title || 'conversation'}`}
        aria-haspopup="menu" onClick={e => { const r = e.currentTarget.getBoundingClientRect(); onMenu(row, r.right, r.bottom) }}>•••</button></div>}
    </div>
  )
}

export function SessionMenu({ row, x, y, sections, siblings, onClose, topbar = false }: {
  row: Row; x: number; y: number; sections: string[]; siblings: Row[]; onClose: () => void; topbar?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [moving, setMoving] = useState(false)
  const [opening, setOpening] = useState(false)
  const transcript = useStore(s => s.transcript)
  const [section, setSection] = useState(row.section)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(row.title)
  const [confirm, setConfirm] = useState(false)
  useEffect(() => {
    ref.current?.querySelector('button')?.focus()
    const outside = (e: PointerEvent): void => { if (!ref.current?.contains(e.target as Node)) onClose() }
    const key = (e: KeyboardEvent): void => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }
    window.addEventListener('pointerdown', outside)
    window.addEventListener('keydown', key)
    return () => { window.removeEventListener('pointerdown', outside); window.removeEventListener('keydown', key) }
  }, [onClose])
  const action = (fn: () => void): void => { fn(); onClose() }
  const index = siblings.findIndex(r => r.id === row.id)
  const move = (delta: number): void => {
    const rows = siblings.filter(r => r.saved)
    const i = rows.findIndex(r => r.id === row.id), j = i + delta
    if (i < 0 || j < 0 || j >= rows.length) return
    ;[rows[i], rows[j]] = [rows[j]!, rows[i]!]
    void Promise.all(rows.map((r, n) => updateSidebar(r.id, { position: n + 1 })))
  }
  const left = Math.max(8, Math.min(x, window.innerWidth - 225))
  const top = Math.max(8, Math.min(y, window.innerHeight - 405))
  return createPortal(<div ref={ref} className="session-menu" role="menu" aria-label="Conversation options" style={{ left, top }}>
    {opening ? <>
      <button role="menuitem" onClick={() => setOpening(false)}>‹ Back</button>
      <button role="menuitem" disabled={row.working} title={row.working ? 'Wait for the conversation to finish' : ''}
        onClick={() => action(() => { void openSessionWindow(row.id) })}>New window</button>
      <button role="menuitem" disabled={!row.cwd} onClick={() => action(() => { void openFolderIn(row.cwd, 'vscode') })}>VS Code</button>
      <button role="menuitem" disabled={!row.cwd} onClick={() => action(() => { void openFolderIn(row.cwd, 'finder') })}>{/Mac/.test(navigator.userAgent) ? 'Finder' : 'File manager'}</button>
    </> : renaming ? <form className="menu-form" onSubmit={e => { e.preventDefault(); if (title.trim()) action(() => { void renameSession(row.id, title) }) }}>
      <label>Rename<input className="input" autoFocus value={title} maxLength={80} onChange={e => setTitle(e.target.value)} /></label>
      <button type="submit">Save</button><button type="button" onClick={() => setRenaming(false)}>Cancel</button>
    </form> : moving ? <form className="menu-form" onSubmit={e => { e.preventDefault(); action(() => { void updateSidebar(row.id, { section: section.trim(), position: 0 }) }) }}>
      <label>Move to section<input className="input" autoFocus list="vesper-sections" value={section} maxLength={80}
        placeholder="Folder default" onChange={e => setSection(e.target.value)} /></label>
      <datalist id="vesper-sections">{sections.map(s => <option key={s} value={s} />)}</datalist>
      <button type="submit">Move</button><button type="button" onClick={() => setMoving(false)}>Cancel</button>
    </form> : confirm ? <div className="menu-form"><span>Delete this conversation permanently?</span>
      <button type="button" className="danger-on" onClick={() => action(() => { void deleteSession(row.id) })}>Delete permanently</button>
      <button type="button" onClick={() => setConfirm(false)}>Cancel</button></div> : <>
      {isDesktop() && <button role="menuitem" onClick={() => setOpening(true)}>Open in ›</button>}
      {!topbar && <>
        <button role="menuitem" onClick={() => action(() => { void updateSidebar(row.id, { pinned: !row.pinned, position: 0 }) })}>{row.pinned ? 'Unpin' : 'Pin'}</button>
        <button role="menuitem" onClick={() => action(() => { void updateSidebar(row.id, { unread: !row.unread }) })}>Mark as {row.unread ? 'read' : 'unread'}</button>
        <button role="menuitem" onClick={() => action(() => { void updateSidebar(row.id, { completed: !row.completed }) })}>Mark as {row.completed ? 'incomplete' : 'completed'}</button>
      </>}
      <button role="menuitem" onClick={() => setRenaming(true)}>Rename</button>
      <button role="menuitem" disabled={row.working} title={row.working ? 'Wait for this conversation to finish' : 'Fork the conversation history (same folder)'}
        onClick={() => action(() => { void forkSession(row.id) })}>Fork</button>
      {topbar ? <>
        <button role="menuitem" onClick={() => action(toggleTranscript)}>Transcript view {transcript ? '✓' : ''}</button>
        <button role="menuitem" onClick={() => action(() => openSettings('appearance'))}>Appearance</button>
      </> : <>
        <button role="menuitem" disabled={index <= 0} onClick={() => action(() => move(-1))}>Move up</button>
        <button role="menuitem" disabled={index < 0 || index >= siblings.length - 1} onClick={() => action(() => move(1))}>Move down</button>
        <button role="menuitem" onClick={() => setMoving(true)}>Move to section</button>
      </>}
      <button role="menuitem" onClick={() => action(() => { void updateSidebar(row.id, { archived: !row.archived }) })}>{row.archived ? 'Unarchive' : 'Archive'}</button>
      <button role="menuitem" className="danger-on" disabled={row.working} title={row.working ? 'Stop this conversation before deleting it' : ''} onClick={() => setConfirm(true)}>Delete</button>
    </>}
  </div>, document.body)
}

export function Sidebar() {
  const history = useStore(s => s.history)
  const loaded = useStore(s => s.historyLoaded)
  const open = useStore(currentChat)
  const chats = useStore(s => s.chats)
  const streamingIds = useStore(s => s.streamingIds)
  const workspace = useStore(s => currentChat(s)?.cwd ?? s.server?.settings.workspace ?? '')
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState<{ query: string; rows: SavedSession[]; error: boolean } | null>(null)
  useEffect(() => {
    const q = query.trim()
    if (!q) return
    let cancelled = false
    const timer = setTimeout(() => {
      void api.sessions(q).then(rows => { if (!cancelled) setSearch({ query: q, rows, error: false }) },
        () => { if (!cancelled) setSearch({ query: q, rows: [], error: true }) })
    }, 250)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [query])
  const [scope, setScope] = useState<'all' | 'here'>('all')
  const [archived, setArchived] = useState(false)
  const [more, setMore] = useState(false)
  const [collapsed, setCollapsed] = useState<string[]>(() => {
    try { const v: unknown = JSON.parse(localStorage.getItem('vesper.sidebar.collapsed') || '[]'); return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [] } catch { return [] }
  })
  const [menu, setMenu] = useState<{ id: string; x: number; y: number } | null>(null)
  const groups = useMemo(() => {
    const openBySession = new Map(chats.filter(c => c.sessionId).map(c => [c.sessionId, c]))
    const openById = new Map(chats.map(c => [c.id, c]))
    const matches = query.trim() === search?.query ? search.rows : []
    const saved = new Map(history.map(h => [h.id, h]))
    for (const h of matches) saved.set(h.id, h)
    const matchedIds = new Set(matches.map(h => h.id))
    const rows: Row[] = [...saved.values()].map(h => {
      const chat = openBySession.get(h.id)
      return { id: h.id, title: chat?.title ?? h.title, updated: chat?.updated ?? h.updated_at * 1000,
        cwd: chat?.cwd ?? h.cwd, source: h.source, saved: true, working: !!h.working || (!!chat && streamingIds.includes(chat.id)),
        needsApproval: !!chat && chat.messages.some(m => m.tools.some(t => t.approval && !t.approval.answer)),
        pinned: !!h.pinned, archived: !!h.archived, unread: !!h.unread, completed: !!h.completed,
        section: h.section || '', position: h.position || 0 }
    })
    const listed = new Set(saved.keys())
    for (const c of chats) if (c.messages.length && !(c.sessionId && listed.has(c.sessionId))) {
      rows.unshift({ id: c.sessionId ?? c.id, title: c.title, updated: c.updated, cwd: c.cwd, source: 'desktop', saved: !!c.sessionId,
        working: streamingIds.includes(c.id), needsApproval: c.messages.some(m => m.tools.some(t => t.approval && !t.approval.answer)),
        pinned: false, archived: false, unread: false, completed: false, section: '', position: 0 })
    }
    const q = query.trim().toLowerCase()
    const list = rows.filter(r => r.archived === archived && (scope === 'all' || r.cwd === workspace))
      .filter(r => { const c = openBySession.get(r.id) ?? openById.get(r.id)
        return !q || matchedIds.has(r.id) || `${r.title} ${r.cwd} ${c ? c.messages.map(m => m.text).join(' ') : ''}`.toLowerCase().includes(q) })
    const out = new Map<string, Row[]>()
    for (const row of list) { const key = row.pinned && !archived ? '\u0000Pinned' : sectionName(row); out.set(key, [...(out.get(key) ?? []), row]) }
    const sorted = [...out.entries()].map(([name, items]): [string, Row[]] => [name, items.sort((a, b) =>
      (a.position || 1_000_001) - (b.position || 1_000_001) || b.updated - a.updated)])
    sorted.sort(([a], [b]) => a === '\u0000Pinned' ? -1 : b === '\u0000Pinned' ? 1 : a.localeCompare(b))
    return sorted
  }, [history, chats, query, search, scope, workspace, streamingIds, archived])
  const activeId = open?.sessionId ?? open?.id
  const menuGroup = groups.find(([, rows]) => rows.some(r => r.id === menu?.id))
  const menuRow = menuGroup?.[1].find(r => r.id === menu?.id)
  const toggle = (label: string): void => setCollapsed(old => {
    const next = old.includes(label) ? old.filter(x => x !== label) : [...old, label]
    try { localStorage.setItem('vesper.sidebar.collapsed', JSON.stringify(next)) } catch { /* private mode */ }
    return next
  })
  return <aside className="sidebar">
    <div className="side-top drag"><Wordmark /></div>
    <button type="button" className="new-chat" onClick={() => { newChat(); window.dispatchEvent(new Event('vesper:focus-composer')) }}>
      <Icon name="plus" size={16} /> New <kbd>{/Mac/.test(navigator.userAgent) ? '⌘N' : 'Ctrl N'}</kbd></button>
    <label className="side-search"><Icon name="search" size={14} /><input value={query} maxLength={200} onChange={e => setQuery(e.target.value)} placeholder="Search conversations" aria-label="Search conversations" /></label>
    <button type="button" className="side-link" onClick={() => openSettings('appearance')}><Icon name="sliders" size={16} /> Customize</button>
    <button type="button" className="side-link" aria-expanded={more} onClick={() => setMore(!more)}><span className="side-more-icon">•••</span> More <Icon name="chevron" size={13} /></button>
    {more && <button type="button" className="side-link side-subitem" onClick={() => { setArchived(!archived); setMenu(null) }}><Icon name="folder" size={15} />{archived ? 'Conversations' : 'Archived'}</button>}
    {!!workspace && <div className="segmented mini side-scope" role="group" aria-label="Which conversations">
      <button type="button" className={scope === 'all' ? 'on' : ''} onClick={() => setScope('all')}>All</button>
      <button type="button" className={scope === 'here' ? 'on' : ''} onClick={() => setScope('here')} title={workspace}>This folder</button></div>}
    <nav className="chat-list" aria-label={archived ? 'Archived conversations' : 'Conversations'}>
      {groups.map(([label, list]) => <div key={label} className="chat-group">
        <button type="button" className="group-label group-toggle" aria-expanded={!!query.trim() || !collapsed.includes(label)} onClick={() => toggle(label)}>
          <span>{label === '\u0000Pinned' ? 'Pinned' : label}</span><Icon name="chevron" size={13} /></button>
        {(!!query.trim() || !collapsed.includes(label)) && list.map(r => <ChatRow key={r.id} row={r} active={r.id === activeId} onMenu={(row, x, y) => setMenu({ id: row.id, x, y })} />)}
      </div>)}
      {loaded && !groups.length && <p className="side-empty">{query.trim() && search?.query !== query.trim() ? 'Searching history…' : search?.query === query.trim() && search.error ? 'History search is unavailable.' : query || scope === 'here' ? 'No conversations match.' : archived ? 'No archived conversations.' : 'Your conversations from the app and the terminal show up here.'}</p>}
    </nav>
    <div className="side-bottom">
      <button type="button" className="side-link" onClick={() => openSettings('models')}><Icon name="gear" size={16} /> Settings</button>
    </div>
    {menu && menuRow && menuGroup && <SessionMenu key={menu.id} row={menuRow} x={menu.x} y={menu.y}
      sections={[...new Set(history.map(h => h.section || (h.cwd ? folderName(h.cwd) : 'Other')))].sort()}
      siblings={menuGroup[1]} onClose={() => setMenu(null)} />}
  </aside>
}
