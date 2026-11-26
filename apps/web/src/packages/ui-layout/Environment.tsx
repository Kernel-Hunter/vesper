import { useEffect, useMemo, useState } from 'react'
import { api, type CheckState, type GitState } from '../connection/api'
import { compactChat, currentBusy, currentChat, folderName, gitAction, isDesktop, openFolderIn, pickFolder, setCheckCommand, useStore } from '../store/store'
import { Icon } from '../ui-primitives/Icon'
import { formatCost } from '../ui-model-selection/catalog'
import { percent, tokenCount } from './context'
import { changeStats, conversationChanges } from './changes'

/** Conversation-specific environment and edits observed through Vesper's file tools. */
export function Environment({ onClose }: { onClose: () => void }) {
  const chat = useStore(currentChat)
  const busy = useStore(currentBusy)
  const home = useStore(s => s.server?.home ?? '')
  const workspace = useStore(s => currentChat(s)?.cwd ?? s.server?.settings.workspace ?? '') || home
  const desktop = isDesktop()
  const [contextDetails, setContextDetails] = useState(() => {
    try { return localStorage.getItem('vesper.contextDetails') === '1' } catch { return false }
  })
  const toggleContextDetails = (): void => {
    const next = !contextDetails
    setContextDetails(next)
    try { localStorage.setItem('vesper.contextDetails', next ? '1' : '0') } catch { /* storage is optional */ }
  }
  const [check, setCheck] = useState<CheckState | null>(null)
  const [editingCheck, setEditingCheck] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    const load = (): void => { api.check(workspace).then(c => { if (live) setCheck(c) }, () => { if (live) setCheck(null) }) }
    load()
    window.addEventListener('vesper:check-changed', load)
    return () => { live = false; window.removeEventListener('vesper:check-changed', load) }
  }, [workspace])
  const [git, setGit] = useState<GitState | null>(null)
  useEffect(() => {
    let live = true
    const load = (): void => { api.git(workspace).then(g => { if (live) setGit(g) }, () => { if (live) setGit(null) }) }
    load()
    window.addEventListener('vesper:git-changed', load)
    return () => { live = false; window.removeEventListener('vesper:git-changed', load) }
  }, [workspace])
  const discard = (): void => {
    const wt = git?.worktree
    if (!wt) return
    const [files, commits] = wt.lost
    const lost = [files && `${files} changed file${files === 1 ? '' : 's'}`, commits && `${commits} unmerged commit${commits === 1 ? '' : 's'}`].filter(Boolean)
    if (window.confirm(`Delete this copy and its branch ${wt.branch}${lost.length ? `, with ${lost.join(' and ')}` : ''}? This cannot be undone.`)) void gitAction('discard')
  }
  const saveCheck = (value: string): void => { setEditingCheck(null); void setCheckCommand(value.trim() || 'off') }
  const changes = useMemo(() => conversationChanges(chat?.messages ?? []), [chat?.messages])
  const stats = useMemo(() => changeStats(chat?.messages ?? []), [chat?.messages])
  const usage = chat?.lastUsage
  const chatCost = useMemo(() => {
    const costs = (chat?.messages ?? []).map(m => m.usage?.cost).filter((c): c is number => typeof c === 'number')
    return costs.length ? costs.reduce((a, b) => a + b, 0) : null
  }, [chat?.messages])
  const report = chat?.contextReport
  const breakdown = report && [
    ['Instructions', report.breakdown.instructions], ['Tool definitions', report.breakdown.tools],
    ['Conversation', report.breakdown.conversation], ['Tool results', report.breakdown.tool_results],
    ['Summary', report.breakdown.summary],
  ] as const
  return <aside className="environment" aria-label="Conversation environment">
    <div className="env-header"><strong>Environment</strong><button type="button" className="icon-btn sm" onClick={onClose} aria-label="Close environment panel"><Icon name="close" size={15} /></button></div>
    <div className="env-section">
      <div className="env-label">Working folder</div>
      <button type="button" className="env-folder" title={workspace || 'Choose a folder'} onClick={() => void pickFolder()}>
        <Icon name="folder" size={16} /><span>{workspace ? folderName(workspace) : 'Choose a folder'}</span><Icon name="chevron" size={13} />
      </button>
      {workspace && <div className="env-path" title={workspace}>{workspace}</div>}
      {desktop && workspace && <div className="env-links">
        <button type="button" onClick={() => void openFolderIn(workspace, 'finder')}>{/Mac/.test(navigator.userAgent) ? 'Finder' : 'File manager'}</button>
        <button type="button" onClick={() => void openFolderIn(workspace, 'vscode')}>VS Code</button>
      </div>}
    </div>
    <div className="env-section">
      <div className="env-label">Context <span>{report && percent(report.used, report.window)}</span></div>
      {report ? <>
        <div className="context-meter" role="progressbar" aria-label="Estimated context used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, Math.round(report.used / report.window * 100))}>
          <span style={{ width: `${Math.min(100, report.used / report.window * 100)}%` }} />
        </div>
        <div className="context-line">
          <span title={`${report.used.toLocaleString()} estimated tokens`}>~{tokenCount(report.used)} of {tokenCount(report.window)}</span>
          {chatCost !== null && <span title="This chat at list prices; cached input is often cheaper">{formatCost(chatCost)}</span>}
          <button type="button" className="context-details-toggle" aria-expanded={contextDetails} onClick={toggleContextDetails}>{contextDetails ? 'Less' : 'Details'}</button>
        </div>
        {contextDetails && <>
        <p className="env-note">{report.model || 'Model'} · {report.window_source === 'documented' ? 'documented window' : report.window_source === 'requested' ? 'requested window; actual limit may differ' : 'assumed window; actual limit unknown'}</p>
        <div className="context-heading">Estimated composition</div>
        <dl className="context-list">{breakdown?.map(([label, count]) => <div key={label}>
          <dt>{label}</dt><dd title={`${count.toLocaleString()} estimated tokens`}>~{tokenCount(count)}</dd>
        </div>)}</dl>
        <p className="env-note">Text-based estimate of retained messages, instructions and tool schemas. Images, provider formatting and hidden reasoning can differ.</p>
        <div className="context-heading">History budget</div>
        <dl className="context-list"><div><dt>Before automatic cleanup</dt><dd>~{tokenCount(Math.max(0, report.history_budget - report.history))}</dd></div></dl>
        <p className="env-note">Vesper summarizes or trims older history after approximately {tokenCount(report.history_budget)} history tokens, reserving space for instructions and replies.</p>
        <button type="button" className="context-compact" disabled={busy} onClick={() => void compactChat()}>Summarize older messages</button>
        </>}
      </> : <p className="env-detail">Available after the first response.</p>}
      {contextDetails && usage && <>
        <div className="context-heading">Last turn · all model calls</div>
        <dl className="context-list">
          <div><dt>Input</dt><dd title={`${usage.input.toLocaleString()} tokens`}>{usage.estimated ? '~' : ''}{tokenCount(usage.input)}</dd></div>
          <div><dt>Output</dt><dd title={`${usage.output.toLocaleString()} tokens`}>{usage.estimated ? '~' : ''}{tokenCount(usage.output)}</dd></div>
          <div><dt>Cache read <small>(included in input)</small></dt><dd title={`${(usage.cached ?? 0).toLocaleString()} tokens`}>{tokenCount(usage.cached ?? 0)}</dd></div>
        </dl>
        {usage.last_call && <p className="env-note">Last model call: {usage.last_call.estimated ? '~' : ''}{tokenCount(usage.last_call.input)} input + {usage.last_call.estimated ? '~' : ''}{tokenCount(usage.last_call.output)} output. Turn input includes repeated context across calls.</p>}
      </>}
    </div>
    <div className="env-section">
      <div className="env-label">Checks {check?.command && <span className="env-ok">on</span>}</div>
      {editingCheck !== null ? (
        <form className="env-check-form" onSubmit={e => { e.preventDefault(); saveCheck(editingCheck) }}>
          <input className="input" autoFocus value={editingCheck} placeholder={check?.suggestion || 'npm test, pytest -q, cargo test'}
            aria-label="Check command" maxLength={500} onChange={e => setEditingCheck(e.target.value)}
            onKeyDown={e => { if (e.key === 'Escape') setEditingCheck(null) }} />
          <div className="env-links"><button type="submit">Save</button><button type="button" onClick={() => setEditingCheck(null)}>Cancel</button></div>
        </form>
      ) : check?.command ? (
        <>
          <button type="button" className="env-check" title="Change the check command" onClick={() => setEditingCheck(check.command)}>
            <Icon name="check" size={14} /><code>{check.command}</code>
          </button>
          <p className="env-note">Runs after Vesper edits files here; failures go back to it, up to 3 times.</p>
          <div className="env-links"><button type="button" onClick={() => saveCheck('')}>Turn off</button></div>
        </>
      ) : (
        <>
          <p className="env-detail">Run tests or lint after every edit, and let Vesper fix what fails.</p>
          <div className="env-links">
            {check?.suggestion && <button type="button" onClick={() => saveCheck(check.suggestion)}>Use <code>{check.suggestion}</code></button>}
            <button type="button" onClick={() => setEditingCheck(check?.suggestion ?? '')}>{check?.suggestion ? 'Other…' : 'Set a command…'}</button>
          </div>
        </>
      )}
    </div>
    {git?.repo && <div className="env-section">
      <div className="env-label">Git {git.auto && <span className="env-ok">auto-commit</span>}</div>
      <div className="env-path" title={git.repo}><Icon name="branch" size={13} /> {git.branch || 'detached'}</div>
      {git.worktree ? <>
        <p className="env-detail">This chat works in its own copy, started from {git.worktree.base || 'HEAD'}. The main checkout and other chats are not touched.</p>
        <div className="env-links">
          <button type="button" disabled={busy} onClick={() => void gitAction('merge')}>Merge into {git.worktree.base || 'main checkout'}</button>
          <button type="button" disabled={busy} onClick={discard}>Discard…</button>
        </div>
      </> : <>
        <p className="env-detail">{git.auto ? 'Each answer that changes files is committed, and Undo takes the commit back.' : 'Commit each answer\'s changes, or give this chat its own copy of the repository.'}</p>
        <div className="env-links">
          <button type="button" disabled={busy} onClick={() => void gitAction(git.auto ? 'auto_off' : 'auto_on')}>{git.auto ? 'Stop committing' : 'Commit each answer'}</button>
          <button type="button" disabled={busy} title="A separate checkout on its own branch: merge it back when the work is done" onClick={() => void gitAction('worktree', chat?.title)}>Work in a separate copy</button>
        </div>
      </>}
      {git.blocked && git.auto && <p className="env-note">Cannot commit now: {git.blocked}.</p>}
    </div>}
    <div className="env-section env-changes">
      <div className="env-label">Changes {changes.length > 0 && <span>{changes.length}</span>}</div>
      {changes.length ? <ul>{changes.map(([path, action]) => <li key={path} title={path}>
        <Icon name="doc" size={14} /><span>{path.split('/').pop()}</span>
        {stats.get(path) ? <small><span className="stat-add">+{stats.get(path)!.add}</span> <span className="stat-del">−{stats.get(path)!.del}</span></small> : <small>{action}</small>}
      </li>)}</ul> : <p className="env-detail">No file edits yet.</p>}
      {changes.length > 0 && <div className="env-links env-review">
        <button type="button" onClick={() => window.dispatchEvent(new Event('vesper:review'))}>Review changes</button>
      </div>}
    </div>
  </aside>
}
