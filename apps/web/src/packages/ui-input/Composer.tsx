import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'
import {
  attachFiles, COMMANDS, currentAttachments, currentBusy, currentChat, currentDraft, currentRemoteBusy, isDesktop, openSettings, pickAttachFolder, pickAttachments, pickFolder, removeAttachment,
  currentQueue, queueMessage, runCommand, saveSettings, send, setDraft, stop, togglePlan, unqueue, useStore,
} from '../store/store'
import { Icon, type IconName } from '../ui-primitives/Icon'
import { ModelPicker } from '../ui-model-selection/ModelPicker'
import { effortLabel, effortSupport } from '../ui-model-selection/effort'
import { api, type Thinking } from '../connection/api'
import { useDictation } from './useDictation'
import { formatBytes } from './dictationProgress'

const MAX_LEN = 100_000
const MODE_LABEL = { ask: 'Ask first', 'auto-edit': 'Auto-edit', yolo: 'Full access', plan: 'Read only' } as const

function AddMenu({ onClose }: { onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const onDown = (e: PointerEvent): void => {
      const t = e.target as Element
      if (ref.current && !ref.current.contains(t) && !t.closest?.('.add-btn')) onClose()
    }
    const onKey = (e: globalThis.KeyboardEvent): void => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey, true)
    return () => { document.removeEventListener('pointerdown', onDown); document.removeEventListener('keydown', onKey, true) }
  }, [onClose])
  const desktop = isDesktop()
  const item = (icon: IconName, label: string, hint: string, run: () => void, disabled = false) => (
    <button type="button" role="menuitem" className="menu-item" disabled={disabled} onClick={() => { onClose(); run() }}>
      <Icon name={icon} size={16} />
      <span><strong>{label}</strong><small>{hint}</small></span>
    </button>
  )
  return (
    <div className="add-menu" ref={ref} role="menu" aria-label="Add to the message">
      {item('paperclip', 'Files', 'Code, text, PDFs or images', () => void pickAttachments(), !desktop)}
      {item('folder', 'A folder', 'Its file list; Vesper can open what is inside', () => void pickAttachFolder(), !desktop)}
      {item('home', 'Work in a folder', 'Make a project the working folder', () => void pickFolder())}
      {!desktop && <p className="menu-note">In the browser, paste images or type @path in the message.</p>}
    </div>
  )
}

export function Composer() {
  const text = useStore(currentDraft)
  const setText = setDraft
  const [picker, setPicker] = useState(false)
  const [adding, setAdding] = useState(false)
  const [cmdIndex, setCmdIndex] = useState(0)
  const ref = useRef<HTMLTextAreaElement>(null)
  const streaming = useStore(currentBusy)
  const remoteBusy = useStore(currentRemoteBusy)
  const settings = useStore(s => s.server?.settings)
  const providers = useStore(s => s.server?.providers)
  const attachments = useStore(currentAttachments)
  const queue = useStore(currentQueue)
  const chatId = useStore(s => currentChat(s)?.id ?? null)
  const draftId = useStore(s => s.currentId ?? s.newDraftId)
  const theme = useStore(s => s.appearance.theme)
  const dictation = useDictation(draftId, spoken => {
    setText(previous => previous + (previous && !/\s$/.test(previous) ? ' ' : '') + spoken)
    ref.current?.focus()
  })

  const provider = providers?.find(p => p.id === settings?.provider)
  const model = settings?.model || provider?.chat_model || ''
  const mode = settings?.agent_mode ?? 'ask'
  const effort = effortSupport(settings?.provider ?? '', model)

  // Slash commands: suggestions while the message is a single /word, the project's own commands included.
  const [custom, setCustom] = useState<{ name: string; hint: string }[]>([])
  const workspace = settings?.workspace
  useEffect(() => {
    let live = true
    api.commands().then(r => {
      if (live) setCustom(r.commands.filter(c => !COMMANDS.some(b => b.name === `/${c.name}`))
        .map(c => ({ name: `/${c.name}`, hint: `${c.hint ? `${c.hint} · ` : ''}${c.description}` })))
    }, () => undefined)
    return () => { live = false }
  }, [workspace])
  const slash = /^\/\S*$/.test(text) ? [...COMMANDS, ...custom].filter(c => c.name.toLowerCase().startsWith(text.toLowerCase())) : []

  // Autosize up to 40% of the viewport.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    const max = Math.round(window.innerHeight * 0.4)
    el.style.height = `${Math.min(el.scrollHeight, max)}px`
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden'
  }, [text])

  useEffect(() => { ref.current?.focus() }, [chatId])
  useEffect(() => {
    const focus = (): void => ref.current?.focus()
    window.addEventListener('vesper:focus-composer', focus)
    return () => window.removeEventListener('vesper:focus-composer', focus)
  }, [])
  useEffect(() => setCmdIndex(0), [text])

  const submit = (): void => {
    const q = text.trim()
    if (streaming && q && !q.startsWith('/') && !attachments.length && q.length <= MAX_LEN && dictation.phase === 'idle') {
      queueMessage(q) // typed while it works: goes out when this answer ends
      setText('')
      return
    }
    if ((!q && !attachments.length) || streaming || q.length > MAX_LEN || dictation.phase !== 'idle') return
    if (q.startsWith('/') && runCommand(q)) { setText('', draftId); return }
    setText('')
    void send(q)
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (slash.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault()
      setCmdIndex(i => (i + (e.key === 'ArrowDown' ? 1 : slash.length - 1)) % slash.length)
      return
    }
    if (slash.length && e.key === 'Tab') {
      e.preventDefault()
      setText(slash[cmdIndex]!.name + ' ')
      return
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (slash.length && text !== slash[cmdIndex]!.name) { setText(slash[cmdIndex]!.name + ' '); return }
      submit()
    }
    if (e.key === 'Backspace' && !text && attachments.length) removeAttachment(attachments[attachments.length - 1]!.key)
  }

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>): void => {
    const files = [...e.clipboardData.files]
    if (files.length) {
      e.preventDefault()
      void attachFiles(files)
    }
  }

  const tooLong = text.length > MAX_LEN
  return (
    <div className="composer-wrap">
      {!!slash.length && (
        <div className="slash-menu" role="listbox" aria-label="Commands">
          {slash.map((c, i) => (
            <button type="button" key={c.name} role="option" aria-selected={i === cmdIndex}
              className={`slash-item${i === cmdIndex ? ' on' : ''}`}
              onMouseEnter={() => setCmdIndex(i)} onClick={() => { setText(c.name + ' '); ref.current?.focus() }}>
              <code>{c.name}</code><span>{c.hint}</span>
            </button>
          ))}
        </div>
      )}
      <form className="composer" onSubmit={e => { e.preventDefault(); submit() }}>
        {!!queue.length && chatId && (
          <div className="queue-row" aria-label="Messages waiting to be sent">
            {queue.map((q, i) => (
              <span key={`${i}:${q}`} className="queue-chip" title="Sent when the current answer ends. Click to edit.">
                <Icon name="up" size={12} />
                <button type="button" className="queue-text" onClick={() => { const t = unqueue(chatId, i); if (t) setText(old => old ? `${old}\n${t}` : t); ref.current?.focus() }}>{q}</button>
                <button type="button" className="att-x" onClick={() => unqueue(chatId, i)} aria-label="Remove from the queue"><Icon name="close" size={12} /></button>
              </span>
            ))}
          </div>
        )}
        {!!attachments.length && (
          <div className="att-row">
            {attachments.map(a => (
              <span key={a.key} className={`att-chip ${a.kind}`} title={a.path ?? a.name}>
                {a.preview ? <img src={a.preview} alt="" /> : <Icon name={a.kind === 'image' ? 'image' : a.kind === 'folder' ? 'folder' : 'doc'} size={14} />}
                <span>{a.name}</span>
                <button type="button" className="att-x" onClick={() => removeAttachment(a.key)} aria-label={`Remove ${a.name}`}>
                  <Icon name="close" size={12} />
                </button>
              </span>
            ))}
          </div>
        )}
        {theme === 'terminal' && <span className="composer-prompt" aria-hidden="true">$</span>}
        <textarea
          ref={ref}
          rows={1}
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          placeholder={streaming ? 'Type the next message: it is sent when this answer ends' : theme === 'terminal' ? 'task --here' : 'Ask Vesper anything · / for commands, @ for files'}
          aria-label="Message"
          spellCheck
        />
        {dictation.phase === 'transcribing' && dictation.progress?.phase === 'loading' && (
          dictation.progress.downloaded < dictation.progress.total ? (
            <div className="dictation-download" role="status">
              <span>Downloading speech model · {formatBytes(dictation.progress.downloaded)} / {formatBytes(dictation.progress.total)}
                {' '}({Math.floor(100 * dictation.progress.downloaded / dictation.progress.total)}%)
                {' '}· {dictation.speed === null ? 'Calculating speed…' : `~${formatBytes(dictation.speed)}/s`}
              </span>
              <progress aria-label="Speech model download" value={dictation.progress.downloaded} max={dictation.progress.total} />
              <span>Estimated download speed; the first download may take a few minutes.</span>
            </div>
          ) : <div className="dictation-status" role="status">Loading speech model…</div>
        )}
        {remoteBusy && <div className="dictation-status" role="status">This conversation is working in another window. You can send when it finishes.</div>}
        <div className="composer-bar">
          <div className="composer-left">
            <button type="button" className={`icon-btn add-btn${adding ? ' on' : ''}`} onClick={() => setAdding(a => !a)}
              aria-haspopup="menu" aria-expanded={adding} aria-label="Add files, a folder or images" title="Add files, a folder or images">
              <Icon name="plus" size={17} />
            </button>
            <button type="button" className="chip-btn" onClick={() => setPicker(p => !p)} aria-haspopup="dialog" aria-expanded={picker}
              title={provider ? `${provider.label} · ${model}. Click to change.` : 'Choose a model'}>
              <span className="chip-label">{provider ? model.split('/').pop() : 'Choose model'}</span>
              <Icon name="chevron" size={12} />
            </button>
            {effort !== 'unavailable' && <label className="effort-control" title={`${effortLabel[effort]}. ${effort === 'unknown' ? 'You can try a level, but the provider may reject or ignore it.' : 'Changes the global setting for future requests.'}`}>
              <Icon name="spark" size={13} />
              <select aria-label={`Reasoning effort: ${effortLabel[effort]}`} value={settings?.thinking ?? 'off'}
                disabled={!settings || streaming}
                onChange={e => void saveSettings({ thinking: e.target.value as Thinking })}>
                <option value="off">Default effort</option><option value="low">Low</option>
                <option value="medium">Medium</option><option value="high">High</option>
              </select>
            </label>}
            <button type="button" className={`chip-btn${mode === 'yolo' ? ' warn' : ''}`} onClick={() => openSettings('agent')} title="What Vesper may do without asking">
              <Icon name={mode === 'yolo' ? 'warn' : 'shield'} size={13} />
              <span className="chip-label">{MODE_LABEL[mode]}</span>
            </button>
            <button type="button" className={`chip-btn toggle${mode === 'plan' ? ' on' : ''}`} onClick={() => void togglePlan()}
              aria-pressed={mode === 'plan'} disabled={streaming}
              title="Plan mode: Vesper reads and proposes a plan, and changes nothing until you approve it">
              <Icon name="list" size={13} /> Plan
            </button>
          </div>
          <div className="composer-right">
            {dictation.phase !== 'idle' && dictation.progress?.phase !== 'loading' && <span className="dictation-status" role="status">
              {dictation.phase === 'recording' ? 'Recording · click mic to finish' : dictation.phase === 'requesting' ? 'Opening microphone…' : dictation.progress?.phase === 'transcribing' ? 'Transcribing locally…' : 'Preparing dictation…'}
            </span>}
            <button type="button" className={`mic-btn${dictation.phase === 'recording' ? ' recording' : ''}`}
              onClick={() => void dictation.toggle()} disabled={streaming || (dictation.phase !== 'idle' && dictation.phase !== 'recording')}
              aria-label={dictation.phase === 'recording' ? 'Finish dictation' : 'Dictate a message'}
              aria-pressed={dictation.phase === 'recording'} title={dictation.phase === 'recording' ? 'Finish dictation' : 'Dictate a message locally'}>
              <Icon name="mic" size={17} />
            </button>
            {tooLong && <span className="count err">{text.length}/{MAX_LEN}</span>}
            {streaming ? (
              <button type="button" className="send stop" onClick={stop} disabled={remoteBusy} aria-label={remoteBusy ? 'Working in another window' : 'Stop'} title={remoteBusy ? 'Stop the response in its window' : 'Stop (Esc)'}>
                <span className="stop-square" />
              </button>
            ) : (
              <button type="submit" className="send" disabled={(!text.trim() && !attachments.length) || tooLong || dictation.phase !== 'idle'} aria-label="Send">
                <Icon name="up" size={17} />
              </button>
            )}
          </div>
        </div>
        {picker && <ModelPicker onClose={() => setPicker(false)} />}
        {adding && <AddMenu onClose={() => setAdding(false)} />}
      </form>
    </div>
  )
}
