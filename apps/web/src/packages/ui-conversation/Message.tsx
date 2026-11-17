import { formatCost } from '../ui-model-selection/catalog'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import type { Task } from '../connection/api'
import type { Msg, Thought } from '../store/store'
import { retry, rewindTo, runPlan, toast, undoChanges, useStore } from '../store/store'
import { TOOL_VERBS, ToolGroup, ToolRow } from './ToolRow'
import { Icon } from '../ui-primitives/Icon'
import { LogoMark } from '../ui-primitives/Logo'
import { onCodeCopy, renderMarkdown } from './markdown'
import { tokenCount as count } from '../ui-layout/context'
const seconds = (s: number): string => (s < 60 ? `${s < 10 ? s.toFixed(1) : Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`)

/** The model's thinking. Open while it is being written and in transcript view, one line otherwise. */
function ThoughtBlock({ thought, live, expanded }: { thought: Thought; live: boolean; expanded: boolean }) {
  const [open, setOpen] = useState<boolean | null>(null)
  const shown = open ?? (live || expanded)
  const words = thought.text.trim().split(/\s+/).length
  return (
    <div className={`thought${live ? ' live' : ''}`}>
      <button type="button" className="thought-head" onClick={() => setOpen(!shown)} aria-expanded={shown}>
        <Icon name="spark" size={13} />
        <span className={live ? 'shimmer' : ''}>{live ? 'Thinking' : `Thought (${words} word${words === 1 ? '' : 's'})`}</span>
        <Icon name="chevron" size={13} className={`tool-chev${shown ? ' rot' : ''}`} />
      </button>
      {shown && <div className="thought-text">{thought.text.trim()}</div>}
    </div>
  )
}

/** The agent's task list: what is done, what it is doing, what is left. */
function TaskList({ items }: { items: Task[] }) {
  const done = items.filter(t => t.status === 'done').length
  return (
    <div className="tasks" aria-label={`Tasks, ${done} of ${items.length} done`}>
      {items.map((t, i) => (
        <div key={i} className={`task ${t.status}`}>
          <span className="mark" aria-hidden="true">{t.status === 'done' ? '✓' : t.status === 'active' ? '▸' : '○'}</span>
          <span>{t.text}</span>
        </div>
      ))}
    </div>
  )
}

function UserMessage({ msg, chatId }: { msg: Msg; chatId: string }) {
  const busy = useStore(s => s.streamingIds.includes(chatId))
  const [mode, setMode] = useState<'view' | 'edit' | 'rewind'>('view')
  const [text, setText] = useState(msg.text)
  const [files, setFiles] = useState(false)
  const [working, setWorking] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    if (mode !== 'edit') return
    const el = box.current
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length) }
  }, [mode])
  const copy = (): void => {
    void navigator.clipboard.writeText(msg.text).then(() => toast('Copied'), () => toast('Copy failed', 'error'))
  }
  const go = async (opts: { files: boolean; edit?: string }): Promise<void> => {
    setWorking(true)
    const ok = await rewindTo(chatId, msg.id, opts)
    setWorking(false)
    if (!ok) return
    setMode('view')
  }
  const filesBox = (
    <label className="rewind-files">
      <input type="checkbox" checked={files} onChange={e => setFiles(e.target.checked)} />
      Also put back files Vesper changed after this message
    </label>
  )
  if (mode === 'edit') {
    return (
      <div className="msg user">
        <div className="user-col editing">
          <textarea ref={box} className="edit-box" value={text} onChange={e => setText(e.target.value)} rows={Math.min(12, text.split('\n').length + 1)}
            aria-label="Edit your message"
            onKeyDown={e => {
              if (e.key === 'Escape') { e.preventDefault(); setMode('view'); setText(msg.text) }
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && text.trim()) { e.preventDefault(); void go({ files, edit: text }) }
            }} />
          {filesBox}
          <div className="edit-actions">
            <span className="dim">Sending forgets this message and everything after it.{msg.attachments?.length ? ' Attachments are not sent again: add them with +.' : ''}</span>
            <button type="button" className="btn" onClick={() => { setMode('view'); setText(msg.text) }}>Cancel</button>
            <button type="button" className="btn primary" disabled={!text.trim() || working || busy} onClick={() => void go({ files, edit: text })}>Send</button>
          </div>
        </div>
      </div>
    )
  }
  return (
    <div className="msg user">
      <span className="prompt" aria-hidden="true">guest@vesper:~$</span>
      <div className="user-col">
        {!!msg.attachments?.length && (
          <div className="sent-attachments">
            {msg.attachments.map(name => (
              <span key={name} className="att-chip sent" title={name}>
                <Icon name={/\.(png|jpe?g|gif|webp)$/i.test(name) ? 'image' : /\.[a-z0-9]{1,6}$/i.test(name) ? 'doc' : 'folder'} size={13} />
                <span>{name}</span>
              </span>
            ))}
          </div>
        )}
        {msg.text && <div className="bubble">{msg.text}</div>}
        {mode === 'rewind' ? (
          <div className="rewind-card" role="dialog" aria-label="Go back to before this message">
            <strong>Go back to before this message?</strong>
            <span className="dim">This message and everything after it are forgotten. Its text goes back into the message box.</span>
            {filesBox}
            <div className="edit-actions">
              <button type="button" className="btn" onClick={() => setMode('view')}>Cancel</button>
              <button type="button" className="btn primary" disabled={working || busy} onClick={() => void go({ files })}>Go back</button>
            </div>
          </div>
        ) : msg.text ? (
          <div className="user-actions">
            <button type="button" className="icon-btn sm" onClick={copy} title="Copy" aria-label="Copy message"><Icon name="copy" size={14} /></button>
            <button type="button" className="icon-btn sm" disabled={busy} onClick={() => { setText(msg.text); setMode('edit') }}
              title={busy ? 'Stop the answer first' : 'Edit and send again'} aria-label="Edit message"><Icon name="edit" size={14} /></button>
            <button type="button" className="icon-btn sm" disabled={busy} onClick={() => setMode('rewind')}
              title={busy ? 'Stop the answer first' : 'Go back to before this message'} aria-label="Rewind to before this message"><Icon name="undo" size={14} /></button>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function BotMessage({ msg, chatId, last }: { msg: Msg; chatId: string; last: boolean }) {
  const transcript = useStore(s => s.transcript)
  const planning = useStore(s => s.server?.settings.agent_mode === 'plan')
  const busy = useStore(s => s.streamingIds.includes(chatId))
  const edited = !msg.undone && msg.tools.some(t => (t.name === 'edit_file' || t.name === 'write_file') && t.ok)
  // Text, thinking and tool calls in the order they happened.
  const parts = useMemo(() => {
    type Part = { kind: 'text'; html: string; key: string } | { kind: 'tool'; index: number; key: string }
      | { kind: 'thought'; index: number; key: string } | { kind: 'group'; indexes: number[]; key: string }
    const out: Part[] = []
    let from = 0
    const steps = [
      ...msg.tools.map((t, index) => ({ kind: 'tool' as const, at: t.at, n: t.n, index, key: `t${t.id}` })),
      ...msg.thoughts.map((t, index) => ({ kind: 'thought' as const, at: t.at, n: t.n, index, key: `h${index}` })),
    ].sort((a, b) => a.at - b.at || a.n - b.n)
    for (const step of steps) {
      const at = Math.min(Math.max(step.at, from), msg.text.length)
      if (at > from && msg.text.slice(from, at).trim()) out.push({ kind: 'text', html: renderMarkdown(msg.text.slice(from, at)), key: `x${from}` })
      out.push({ kind: step.kind, index: step.index, key: step.key })
      from = at
    }
    if (msg.text.slice(from).trim()) out.push({ kind: 'text', html: renderMarkdown(msg.text.slice(from)), key: `x${from}` })
    // Steps in a row fold into one line; a step that waits for approval stays on its own.
    const folded: Part[] = []
    for (const p of out) {
      const prev = folded[folded.length - 1]
      const lone = p.kind === 'tool' && !!msg.tools[p.index]!.approval
      if (p.kind === 'tool' && !lone && prev?.kind === 'tool' && !msg.tools[prev.index]!.approval) {
        folded[folded.length - 1] = { kind: 'group', indexes: [prev.index, p.index], key: prev.key }
      } else if (p.kind === 'tool' && !lone && prev?.kind === 'group') {
        prev.indexes.push(p.index)
      } else folded.push(p)
    }
    return folded
  }, [msg.text, msg.tools, msg.thoughts])
  const lastText = parts.map(p => p.kind).lastIndexOf('text')
  const running = msg.tools.some(t => t.output === undefined)
  const lastKind = parts[parts.length - 1]?.kind
  const thinking = msg.streaming && !msg.error && !running && (parts.length === 0 || lastKind === 'tool' || lastKind === 'group' || !!msg.draft)

  const copy = (): void => {
    void navigator.clipboard.writeText(msg.text).then(() => toast('Copied'), () => toast('Copy failed', 'error'))
  }

  return (
    <div className="msg bot">
      <div className="body">
        {parts.map((p, i) => (p.kind === 'group'
          ? <ToolGroup key={p.key} steps={p.indexes.map(n => msg.tools[n]!)} chatId={chatId} msgId={msg.id} expanded={transcript} />
          : p.kind === 'tool'
          ? <ToolRow key={p.key} step={msg.tools[p.index]!} chatId={chatId} msgId={msg.id} expanded={transcript} />
          : p.kind === 'thought'
            ? (
              <ThoughtBlock
                key={p.key} thought={msg.thoughts[p.index]!} expanded={transcript}
                live={!!msg.streaming && i === parts.length - 1 && !msg.draft}
              />
            )
            : (
            <div
              key={p.key}
              className={`md${msg.streaming && i === lastText && i === parts.length - 1 ? ' cursor' : ''}`}
              onClick={onCodeCopy} onKeyDown={onCodeCopy}
              dangerouslySetInnerHTML={{ __html: p.html }}
            />
          )))}
        {!!msg.tasks?.length && <TaskList items={msg.tasks} />}
        {thinking && (
          <div className="thinking">
            <span className="think-mark" aria-hidden="true"><LogoMark size={16} blink /></span>
            <span className="shimmer">
              {msg.draft ? `Preparing ${TOOL_VERBS[msg.draft.name] ?? msg.draft.name} (${count(msg.draft.chars)} characters)`
                : msg.tools.length ? 'Thinking' : 'Working on it'}
            </span>
          </div>
        )}
        {msg.error && (
          <div className="msg-error" role="alert">
            <Icon name="warn" size={15} />
            <span>{msg.error}</span>
          </div>
        )}
        {last && !msg.streaming && !msg.error && msg.plan && planning && !busy && (
          <div className="plan-bar">
            <button type="button" className="btn primary" onClick={() => void runPlan()}>Run this plan</button>
            <span className="dim">or say what to change. Nothing has been changed yet.</span>
          </div>
        )}
        {!msg.streaming && (
          <div className={`actions${last ? ' show' : ''}`}>
            {msg.text && <button type="button" className="icon-btn sm" onClick={copy} title="Copy" aria-label="Copy answer"><Icon name="copy" size={15} /></button>}
            {last && !busy && <button type="button" className="icon-btn sm" onClick={() => void retry(chatId, msg.id)} title="Ask again" aria-label="Ask again"><Icon name="retry" size={15} /></button>}
            {last && edited && !busy && (
              <button type="button" className="icon-btn sm" onClick={() => void undoChanges()} title="Undo the file changes of this answer" aria-label="Undo file changes"><Icon name="undo" size={15} /></button>
            )}
            {msg.usage && <span className="usage">
              {seconds(msg.usage.seconds)}
              {msg.usage.tools ? ` · ${msg.usage.tools} tool call${msg.usage.tools === 1 ? '' : 's'}` : ''}
              {` · ${msg.usage.estimated ? '~' : ''}${count(msg.usage.input)} in${msg.usage.cached ? ` (${count(msg.usage.cached)} cached)` : ''}, ${msg.usage.estimated ? '~' : ''}${count(msg.usage.output)} out`}
              {msg.usage.cost != null && <span title="About this much at the provider's list prices; cached input is often cheaper">{` · ${formatCost(msg.usage.cost)}`}</span>}
              {msg.undone ? ' · changes undone' : ''}
            </span>}
          </div>
        )}
      </div>
    </div>
  )
}

export const Message = memo(function Message({ msg, chatId, last }: { msg: Msg; chatId: string; last: boolean }) {
  return msg.role === 'user' ? <UserMessage msg={msg} chatId={chatId} /> : <BotMessage msg={msg} chatId={chatId} last={last} />
})
