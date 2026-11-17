import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { currentBusy, currentChat, folderName, pickFolder, send, useStore } from '../store/store'
import { Icon, type IconName } from '../ui-primitives/Icon'
import { LogoMark } from '../ui-primitives/Logo'
import { Message } from './Message'

const SUGGESTIONS: { label: string; prompt: string; icon: IconName }[] = [
  { label: 'Explain this project', prompt: 'Explain what this project does and how it is organized', icon: 'doc' },
  { label: 'Find and fix a bug', prompt: 'Find a bug in this code and fix it', icon: 'search' },
  { label: 'Write tests', prompt: 'Write tests for the most important part', icon: 'check' },
  { label: 'Start something new', prompt: 'Set up a new project here: ask me what it should do', icon: 'spark' },
]

function greeting(): string {
  const h = new Date().getHours()
  return h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

function Empty() {
  const workspace = useStore(s => s.server?.settings.workspace ?? '')
  const terminal = useStore(s => s.appearance.theme === 'terminal')
  if (terminal) {
    return (
      <div className="empty">
        <pre className="boot" aria-label="Vesper ready">{`VESPER :: coding agent
> cwd ................ ${workspace || '~'}
> tools .............. read, search, edit, run, web
> type a task and press ENTER`}</pre>
      </div>
    )
  }
  return (
    <div className="empty">
      <h1 className="hero-title">
        <LogoMark size={40} className="empty-logo" blink />
        <span>{greeting()}{workspace ? <>. What are we building in <em>{folderName(workspace)}</em>?</> : '. What are we building?'}</span>
      </h1>
    </div>
  )
}

/** Quick starts under the message box of an empty chat. */
export function HeroSuggestions() {
  const workspace = useStore(s => s.server?.settings.workspace ?? '')
  return (
    <div className="hero-after">
      <div className="suggestions">
        {SUGGESTIONS.map(q => (
          <button type="button" key={q.label} className="suggestion" title={q.prompt} onClick={() => void send(q.prompt)}>
            <Icon name={q.icon} size={14} />{q.label}
          </button>
        ))}
        {!workspace && (
          <button type="button" className="suggestion" onClick={() => void pickFolder()}>
            <Icon name="folder" size={14} />Open a project folder
          </button>
        )}
      </div>
    </div>
  )
}

export function Thread() {
  const chat = useStore(currentChat)
  const streaming = useStore(currentBusy)
  const ref = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const [showJump, setShowJump] = useState(false)

  const onScroll = (): void => {
    const el = ref.current
    if (!el) return
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stick.current = near
    setShowJump(!near)
  }

  // New chat: jump to bottom.
  useLayoutEffect(() => {
    stick.current = true
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
    setShowJump(false)
  }, [chat?.id])

  // Follow the stream only while the user stays near the bottom.
  useEffect(() => {
    const el = ref.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  })

  const jump = (): void => {
    const el = ref.current
    if (!el) return
    stick.current = true
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }

  if (!chat || !chat.messages.length) {
    return <div className="thread"><Empty /></div>
  }
  return (
    <div className="thread" ref={ref} onScroll={onScroll}>
      <div className="col" role="log" aria-live="polite" aria-busy={!!streaming}>
        {chat.messages.map((m, i) => (
          <Message key={m.id} msg={m} chatId={chat.id} last={i === chat.messages.length - 1} />
        ))}
      </div>
      {showJump && (
        <button type="button" className="jump" onClick={jump} aria-label="Scroll to latest">
          <Icon name="chevron" size={16} />
        </button>
      )}
    </div>
  )
}
