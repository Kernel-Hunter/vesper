import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type ChangedFile } from '../connection/api'
import { currentBusy, currentChat, setDraft, toast, useStore } from '../store/store'
import { Icon } from '../ui-primitives/Icon'
import { diffRows } from '../ui-conversation/diffStats'

const baseName = (path: string): string => path.split(/[\\/]/).pop() || path

/** Every file the current chat changed, compared with what it held before the chat touched it. */
export function Review({ onClose }: { onClose: () => void }) {
  const chat = useStore(currentChat)
  const busy = useStore(currentBusy)
  const [files, setFiles] = useState<ChangedFile[] | null>(null)
  const [selected, setSelected] = useState('')
  const [comment, setComment] = useState<{ row: number; text: string } | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [error, setError] = useState('')
  const chatId = chat?.id
  const folder = useStore(s => currentChat(s)?.cwd ?? s.server?.settings.workspace ?? '')
  const sessionId = chat?.sessionId

  const load = useCallback((): void => {
    if (!chatId) { setFiles([]); return }
    api.changes(chatId, sessionId).then(list => { setFiles(list); setError('') },
      e => { setFiles([]); setError(e instanceof Error ? e.message : 'Could not load the changes.') })
  }, [chatId, sessionId])

  useEffect(() => { if (!busy) load() }, [busy, load])
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])

  const file = files?.find(f => f.path === selected) ?? files?.[0]
  const rows = useMemo(() => (file ? diffRows(file.diff) : []), [file])
  useEffect(() => { setComment(null); setConfirm(false) }, [file?.path])
  const totals = (files ?? []).reduce((t, f) => ({ add: t.add + f.add, del: t.del + f.del }), { add: 0, del: 0 })

  const addComment = (): void => {
    if (!file || !comment?.text.trim()) return
    const row = rows[comment.row]
    const line = row?.now ?? row?.old
    const prefix = folder.replace(/[\\/]+$/, '') + '/'
    const shown = folder && file.path.startsWith(prefix) ? file.path.slice(prefix.length) : file.path
    const note = `In ${shown}${line ? ` at line ${line}` : ''}: ${comment.text.trim()}`
    setDraft(old => (old.trim() ? `${old.trimEnd()}\n${note}` : note))
    setComment(null)
    toast('Added to your message')
    window.dispatchEvent(new Event('vesper:focus-composer'))
  }

  const revert = async (): Promise<void> => {
    if (!file || !chatId) return
    setConfirm(false)
    try {
      await api.revertFile(chatId, file.path, sessionId)
      toast(`Reverted ${baseName(file.path)}`)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not revert the file.', 'error')
    }
    load()
  }

  return (
    <div className="review-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <section className="review" role="dialog" aria-modal="true" aria-label="Review changes">
        <header className="review-head">
          <strong>Review changes</strong>
          {files && files.length > 0 && <span className="review-stat">
            {files.length} {files.length === 1 ? 'file' : 'files'} · <span className="stat-add">+{totals.add}</span> <span className="stat-del">−{totals.del}</span>
          </span>}
          <span className="review-spacer" />
          <button type="button" className="icon-btn sm" onClick={load} aria-label="Refresh" title="Refresh"><Icon name="retry" size={15} /></button>
          <button type="button" className="icon-btn sm" onClick={onClose} aria-label="Close review"><Icon name="close" size={15} /></button>
        </header>
        {files === null ? <div className="review-empty">Loading…</div>
          : !files.length ? <div className="review-empty">{error || 'This conversation has not changed any files yet.'}</div>
          : <div className="review-body">
            <ul className="review-files">
              {files.map(f => <li key={f.path}>
                <button type="button" className={`review-file${f === file ? ' on' : ''}`} title={f.path} onClick={() => setSelected(f.path)}>
                  <span className={`review-status ${f.status}`}>{f.status === 'added' ? 'A' : f.status === 'deleted' ? 'D' : 'M'}</span>
                  <span className="review-name">{baseName(f.path)}</span>
                  <small><span className="stat-add">+{f.add}</span> <span className="stat-del">−{f.del}</span></small>
                </button>
              </li>)}
            </ul>
            {file && <div className="review-diff">
              <div className="review-file-head">
                <code title={file.path}>{file.path}</code>
                {confirm ? <span className="review-confirm">Restore the file as it was before this chat?
                  <button type="button" onClick={() => void revert()}>Yes</button>
                  <button type="button" onClick={() => setConfirm(false)}>No</button>
                </span> : <button type="button" className="review-revert" disabled={busy} onClick={() => setConfirm(true)}
                  title={busy ? 'Wait for Vesper to finish' : 'Undo every change this chat made to the file'}>
                  <Icon name="undo" size={14} />Revert file
                </button>}
              </div>
              {file.binary ? <div className="review-empty">Binary file: no text diff.</div>
                : !rows.length ? <div className="review-empty">{file.status === 'deleted' ? 'Empty file deleted.' : 'No text changes.'}</div>
                : <div className="review-lines" role="table">
                  {rows.map((r, i) => <div key={i}>
                    <div className={`rl ${r.kind}`} role="row">
                      <span className="rl-num">{r.old ?? ''}</span>
                      <span className="rl-num">{r.now ?? ''}</span>
                      <span className="rl-sign">{r.kind === 'add' ? '+' : r.kind === 'del' ? '−' : ''}</span>
                      <span className="rl-text">{r.text || ' '}</span>
                      {r.kind !== 'hunk' && <button type="button" className="rl-add" aria-label="Comment on this line"
                        onClick={() => setComment({ row: i, text: '' })}>+</button>}
                    </div>
                    {comment?.row === i && <div className="rl-comment">
                      <textarea autoFocus rows={2} value={comment.text} placeholder="What should Vesper change here?"
                        onChange={e => setComment({ row: i, text: e.target.value })}
                        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); addComment() } }} />
                      <div>
                        <button type="button" onClick={() => setComment(null)}>Cancel</button>
                        <button type="button" className="primary" disabled={!comment.text.trim()} onClick={addComment}>Add to message</button>
                      </div>
                    </div>}
                  </div>)}
                </div>}
            </div>}
          </div>}
      </section>
    </div>
  )
}
