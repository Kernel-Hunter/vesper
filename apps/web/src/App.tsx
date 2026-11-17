import { useEffect, useRef, useState } from 'react'
import {
  attachFiles, closeSettings, currentBusy, currentDraft, getState, newChat, openSession, openSettings, refresh, stop, toggleSidebar, toggleTranscript, useStore,
} from './packages/store/store'
import { Effects } from './packages/ui-effects/Effects'
import { Composer } from './packages/ui-input/Composer'
import { Sidebar } from './packages/ui-layout/Sidebar'
import { Topbar } from './packages/ui-layout/Topbar'
import { Environment } from './packages/ui-layout/Environment'
import { Review } from './packages/ui-layout/Review'
import { Thread } from './packages/ui-conversation/Thread'
import { Settings } from './packages/ui-settings/Settings'
import { Icon } from './packages/ui-primitives/Icon'
import { Toasts } from './packages/ui-primitives/Toasts'

import './packages/ui-layout/layout.css'
import './packages/ui-layout/review.css'
import './packages/ui-conversation/conversation.css'
import './packages/ui-input/input.css'
import './packages/ui-model-selection/model.css'
import './packages/ui-settings/settings.css'
import './packages/ui-effects/effects.css'
import './packages/ui-theme/designs.css'

export function App() {
  const sidebar = useStore(s => s.sidebar)
  const [dragging, setDragging] = useState(false)
  const [environment, setEnvironment] = useState(() => { try { return localStorage.getItem('vesper.environment') !== '0' } catch { return true } })
  const toggleEnvironment = (): void => setEnvironment(open => {
    try { localStorage.setItem('vesper.environment', open ? '0' : '1') } catch { /* private mode */ }
    return !open
  })
  const [review, setReview] = useState(false)
  const depth = useRef(0)

  useEffect(() => {
    const open = (): void => setReview(true)
    window.addEventListener('vesper:review', open)
    return () => window.removeEventListener('vesper:review', open)
  }, [])

  useEffect(() => {
    const url = new URL(window.location.href)
    const sessionId = url.searchParams.get('session')
    url.searchParams.delete('session')
    if (sessionId) window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
    void refresh().then(() => {
      if (sessionId && /^[0-9a-f]{4,32}$/.test(sessionId) && !getState().currentId && !currentDraft(getState())) void openSession(sessionId)
    })
    if (window.innerWidth < 820 && getState().sidebar) toggleSidebar()
  }, [])

  // Keyboard shortcuts.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const mod = e.metaKey || e.ctrlKey
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); window.dispatchEvent(new Event('vesper:focus-composer')) }
      else if (mod && e.key === ',') { e.preventDefault(); if (getState().settings.open) closeSettings(); else openSettings() }
      else if (mod && e.key.toLowerCase() === 'b') { e.preventDefault(); toggleSidebar() }
      else if (mod && !e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); toggleTranscript() }
      else if (mod && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        if (!getState().sidebar) toggleSidebar()
        requestAnimationFrame(() => (document.querySelector('.side-search input') as HTMLInputElement | null)?.focus())
      }
      else if (mod && e.shiftKey && e.key.toLowerCase() === 'd') { e.preventDefault(); setReview(open => !open) }
      else if (e.key === 'Escape' && currentBusy(getState()) && !getState().settings.open) stop()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Drop files, folders or images anywhere to attach them to the next message.
  useEffect(() => {
    const hasFiles = (e: DragEvent): boolean => !!e.dataTransfer?.types.includes('Files')
    const enter = (e: DragEvent): void => { if (!hasFiles(e)) return; e.preventDefault(); depth.current++; setDragging(true) }
    const over = (e: DragEvent): void => { if (hasFiles(e)) e.preventDefault() }
    const leave = (e: DragEvent): void => { if (!hasFiles(e)) return; depth.current = Math.max(0, depth.current - 1); if (!depth.current) setDragging(false) }
    const drop = (e: DragEvent): void => {
      if (!hasFiles(e)) return
      e.preventDefault()
      depth.current = 0
      setDragging(false)
      if (getState().settings.open) return // the dialog's own drop zone handles it
      void attachFiles([...(e.dataTransfer?.files ?? [])])
      window.dispatchEvent(new Event('vesper:focus-composer'))
    }
    window.addEventListener('dragenter', enter)
    window.addEventListener('dragover', over)
    window.addEventListener('dragleave', leave)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragenter', enter)
      window.removeEventListener('dragover', over)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('drop', drop)
    }
  }, [])

  return (
    <>
      <div className={`app${sidebar ? '' : ' no-side'}`} id="app">
        <Sidebar />
        <main className="main" onClick={e => { if (window.innerWidth < 820 && sidebar && e.target === e.currentTarget) toggleSidebar() }}>
          <Effects />
          <Topbar environment={environment} onToggleEnvironment={toggleEnvironment} />
          <Thread />
          <Composer />
        </main>
        {environment && <Environment onClose={toggleEnvironment} />}
      </div>
      {review && <Review onClose={() => setReview(false)} />}
      <Settings />
      {dragging && (
        <div className="drop-overlay"><div><Icon name="paperclip" size={34} />Drop to attach to your message</div></div>
      )}
      <Toasts />
    </>
  )
}
