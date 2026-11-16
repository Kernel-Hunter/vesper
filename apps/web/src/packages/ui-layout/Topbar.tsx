import { useState } from 'react'
import { currentChat, folderName, pickFolder, refresh, toggleSidebar, toggleTranscript, useStore } from '../store/store'
import { Icon } from '../ui-primitives/Icon'
import { SessionMenu, type Row } from './Sidebar'

export function Topbar({ environment, onToggleEnvironment }: { environment: boolean; onToggleEnvironment: () => void }) {
  const chat = useStore(currentChat)
  const history = useStore(s => s.history)
  const streamingIds = useStore(s => s.streamingIds)
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)
  const title = chat?.title ?? ''
  const workspace = useStore(s => currentChat(s)?.cwd ?? s.server?.settings.workspace ?? '')
  const home = useStore(s => s.server?.home ?? '')
  const connected = useStore(s => s.connected)
  const sidebar = useStore(s => s.sidebar)
  const transcript = useStore(s => s.transcript)
  const mac = /Mac/.test(navigator.userAgent)
  const where = workspace || home
  const saved = history.find(h => h.id === chat?.sessionId)
  const row: Row | null = chat?.sessionId ? {
    id: chat.sessionId, title: chat.title, updated: chat.updated, cwd: chat.cwd, source: saved?.source ?? 'desktop',
    saved: true, working: !!saved?.working || streamingIds.includes(chat.id), needsApproval: false, pinned: !!saved?.pinned,
    archived: !!saved?.archived, unread: !!saved?.unread, completed: !!saved?.completed,
    section: saved?.section ?? '', position: saved?.position ?? 0,
  } : null
  return (
    <header className="topbar drag">
      <button type="button" className={`icon-btn sidebar-toggle${sidebar ? '' : ' closed'}`} onClick={toggleSidebar} aria-label={sidebar ? 'Hide sidebar' : 'Show sidebar'} aria-expanded={sidebar}>
        <Icon name="sidebar" size={18} />
      </button>
      <div className="top-title" title={title}>{title}</div>
      <div className="top-right">
        {!connected && (
          <button type="button" className="status err" onClick={() => void refresh()}><span className="pulse" />Engine offline · retry</button>
        )}
        <button type="button" className={`status view${transcript ? ' ok' : ''}`} onClick={toggleTranscript} aria-pressed={transcript}
          title={`Transcript view: show thinking, commands, edits and output in full (${mac ? '⌘' : 'Ctrl+'}O)`}>
          <Icon name={transcript ? 'eye' : 'eyeOff'} size={13} />Transcript
        </button>
        <button type="button" className={`status folder${workspace ? ' ok' : ''}`} onClick={() => void pickFolder()}
          title={`${where || 'Choose a working folder'}\nClick to work in another folder`}>
          <Icon name={workspace ? 'folder' : 'home'} size={13} />{workspace ? folderName(workspace) : 'Home folder'}
        </button>
        {row && <button type="button" className="icon-btn sm top-options" aria-label="Conversation options" aria-haspopup="menu"
          onClick={e => { const r = e.currentTarget.getBoundingClientRect(); setMenu({ x: r.left - 190, y: r.bottom }) }}>•••</button>}
        <button type="button" className={`icon-btn sm env-toggle${environment ? ' on' : ''}`} onClick={onToggleEnvironment}
          aria-label={environment ? 'Hide environment' : 'Show environment'} aria-expanded={environment} title="Environment">
          <Icon name="sidebar" size={17} />
        </button>
      </div>
      {menu && row && <SessionMenu key={row.id} row={row} x={menu.x} y={menu.y} sections={[]} siblings={[]} topbar onClose={() => setMenu(null)} />}
    </header>
  )
}
