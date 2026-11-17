import { useState } from 'react'
import type { ApprovalAnswer } from '../connection/api'
import { respond, type ToolStep } from '../store/store'
import { Icon, type IconName } from '../ui-primitives/Icon'
import { argsDiff, diffStats } from './diffStats'

export { diffStats }

const TOOLS: Record<string, { verb: string; icon: IconName; arg: string }> = {
  list_files: { verb: 'List', icon: 'folder', arg: 'path' },
  read_file: { verb: 'Read', icon: 'doc', arg: 'path' },
  glob: { verb: 'Find', icon: 'search', arg: 'pattern' },
  grep: { verb: 'Search', icon: 'search', arg: 'pattern' },
  change_directory: { verb: 'Go to', icon: 'folder', arg: 'path' },
  write_file: { verb: 'Write', icon: 'edit', arg: 'path' },
  edit_file: { verb: 'Edit', icon: 'edit', arg: 'path' },
  run_command: { verb: 'Run', icon: 'terminal', arg: 'command' },
  web_fetch: { verb: 'Fetch', icon: 'globe', arg: 'url' },
  web_search: { verb: 'Search the web', icon: 'globe', arg: 'query' },
  update_tasks: { verb: 'Tasks', icon: 'list', arg: '' },
  command_output: { verb: 'Check output', icon: 'terminal', arg: 'job' },
  run_check: { verb: 'Check', icon: 'check', arg: 'command' },
  mcp_find: { verb: 'Find tools', icon: 'plug', arg: 'query' },
  mcp_call: { verb: 'Use', icon: 'plug', arg: 'tool' },
}

export const TOOL_VERBS: Record<string, string> = Object.fromEntries(Object.entries(TOOLS).map(([name, t]) => [name, t.verb]))

const short = (text: string, max: number): string => (text.length > max ? `${text.slice(0, max - 1)}…` : text)

/** One line under the tool name: what came back, in a few words. */
function summary(step: ToolStep): string {
  const out = step.output ?? ''
  if (step.approval?.answer === 'no' && /declined/i.test(out)) return 'Declined'
  if (!step.ok) return short(out.replace(/^Error:\s*/, ''), 140)
  const lines = out.split('\n').filter(Boolean)
  switch (step.name) {
    case 'read_file': return `${lines.length} line${lines.length === 1 ? '' : 's'}`
    case 'list_files':
    case 'glob': return out.startsWith('No ') ? short(out, 100) : `${lines.length} item${lines.length === 1 ? '' : 's'}`
    case 'grep': return out.startsWith('No ') ? short(out, 100) : `${lines.length} match${lines.length === 1 ? '' : 'es'}`
    case 'web_fetch': return `${out.length.toLocaleString()} characters`
    case 'web_search': return `${lines.filter(l => /^\d+\. /.test(l)).length} results`
    case 'run_command': case 'run_check': return short(lines[0] ?? 'done', 100)
    default: return short(lines[0] ?? 'done', 120)
  }
}

function Diff({ text }: { text: string }) {
  const lines = text.split('\n')
  return (
    <pre className="diff" aria-label="Proposed change">
      {lines.map((line, i) => {
        const cls = line.startsWith('+++') || line.startsWith('---') ? 'meta'
          : line.startsWith('@@') ? 'hunk' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : ''
        return <span key={i} className={`ln ${cls}`}>{line || ' '}</span>
      })}
    </pre>
  )
}

/** What the call was asked to do, in full: the command line, or the edit as a diff. */
function Detail({ step }: { step: ToolStep }) {
  if (step.name === 'run_command' || step.name === 'run_check') return <pre className="diff cmd">$ {String(step.args.command ?? '')}</pre>
  const diff = step.diff ?? step.approval?.diff ?? argsDiff(step)
  return diff ? <Diff text={diff} /> : null
}

function Approval({ step, chatId, msgId }: { step: ToolStep; chatId: string; msgId: string }) {
  const a = step.approval!
  const answer = (value: ApprovalAnswer): void => { void respond(chatId, msgId, a.id, value) }
  const waiting = !a.answer
  const always = a.kind === 'edit' ? { label: 'Always allow edits', title: 'Edit files without asking for the rest of this chat' }
    : a.kind === 'web' ? { label: `Always allow ${a.host ?? 'this site'}`, title: 'Read pages from this site without asking, from now on' }
      : a.kind === 'mcp' ? { label: `Always allow ${a.mcp_tool ?? 'this tool'}`, title: `Use ${a.mcp_tool ?? 'this tool'} of ${a.server ?? 'this server'} without asking, from now on. Remove the rule in Settings.` }
      : a.rule ? { label: `Always allow ${a.rule}`, title: `Run commands that start with "${a.rule}" without asking, from now on. Remove the rule in Settings.` }
        : { label: 'Always allow this command', title: 'Run this exact command without asking for the rest of this chat' }
  return (
    <div className={`approval${waiting ? ' waiting' : ''}`} role={waiting ? 'alertdialog' : undefined} aria-label="Approve this action">
      {a.kind === 'web'
        ? (
          <>
            <div className="approval-title"><Icon name="globe" size={14} />Read a web page</div>
            <pre className="diff cmd">{a.url}</pre>
          </>
        )
        : a.kind === 'mcp'
        ? (
          <>
            <div className="approval-title"><Icon name="plug" size={14} />Use <code>{a.mcp_tool}</code><span className="dim"> from {a.server}</span></div>
            {a.args && a.args !== '{}' ? <pre className="diff cmd">{a.args}</pre> : null}
          </>
        )
        : a.kind === 'run'
        ? (
          <>
            <div className="approval-title"><Icon name="terminal" size={14} />Run a command{a.cwd ? <span className="dim"> in {a.cwd}</span> : null}</div>
            <pre className="diff cmd">$ {a.command}</pre>
          </>
        )
        : (
          <>
            <div className="approval-title"><Icon name="edit" size={14} />{step.name === 'write_file' ? 'Write' : 'Edit'} <code>{a.path}</code></div>
            {a.diff ? <Diff text={a.diff} /> : <p className="dim">No changes.</p>}
          </>
        )}
      {waiting
        ? (
          <div className="approval-actions">
            <button type="button" className="btn primary" disabled={a.sending} onClick={() => answer('yes')} autoFocus>Allow</button>
            {a.strict
              ? <span className="dim">Needs a yes every time.</span>
              : (
                <button type="button" className="btn" disabled={a.sending} onClick={() => answer('always')} title={always.title}>
                  {always.label}
                </button>
              )}
            <button type="button" className="btn ghost danger" disabled={a.sending} onClick={() => answer('no')}>Deny</button>
          </div>
        )
        : <div className={`approval-result ${a.answer === 'no' ? 'bad' : 'ok'}`}>{a.answer === 'no' ? 'Denied' : a.answer === 'always' ? 'Always allowed' : 'Allowed'}</div>}
    </div>
  )
}

const took = (ms: number): string => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`)

export function ToolRow({ step, chatId, msgId, expanded = false }: { step: ToolStep; chatId: string; msgId: string; expanded?: boolean }) {
  // Closed by default, open in transcript view; a click overrides either.
  const [choice, setOpen] = useState<boolean | null>(null)
  const open = choice ?? expanded
  const info = TOOLS[step.name] ?? { verb: step.name, icon: 'bolt' as IconName, arg: '' }
  const arg = info.arg ? String(step.args[info.arg] ?? '') : ''
  const running = step.output === undefined
  const waiting = !!step.approval && !step.approval.answer
  const state = running ? (waiting ? 'wait' : 'run') : step.ok ? 'ok' : 'bad'
  const stats = diffStats(step)
  return (
    <div className={`tool ${state}`}>
      {/* While an approval card is up, it carries the title; the row header would repeat it. */}
      {!waiting && <button type="button" className="tool-head" onClick={() => setOpen(!open)} aria-expanded={open} disabled={running}>
        <Icon name={info.icon} size={14} />
        <span className="tool-verb">{info.verb}</span>
        {arg && <code className="tool-arg" title={arg}>{short(arg, 90)}</code>}
        {!running && (stats && step.ok
          ? <span className="tool-sum"><span className="stat-add">+{stats.add}</span> <span className="stat-del">−{stats.del}</span></span>
          : <span className="tool-sum">{summary(step)}{step.ms !== undefined && step.ms >= 1000 ? ` · ${took(step.ms)}` : ''}</span>)}
        {running && !waiting && <span className="tool-sum shimmer">working</span>}
        {!running && <Icon name="chevron" size={13} className={`tool-chev${open ? ' rot' : ''}`} />}
      </button>}
      {step.approval && step.approval.answer === undefined && <Approval step={step} chatId={chatId} msgId={msgId} />}
      {/* A running command: its output as it arrives, whatever the view. */}
      {running && step.live && <pre className="tool-out live" aria-live="off">{step.live}</pre>}
      {open && !(step.approval && step.approval.answer === undefined) && <Detail step={step} />}
      {open && step.approval?.answer && (
        <div className={`approval-result ${step.approval.answer === 'no' ? 'bad' : 'ok'}`}>
          {step.approval.answer === 'no' ? 'Denied' : step.approval.answer === 'always' ? 'Always allowed' : 'Allowed'}
        </div>
      )}
      {open && step.output && <pre className="tool-out">{short(step.output, 4000)}</pre>}
    </div>
  )
}

type Kind = 'read' | 'search' | 'edit' | 'run' | 'check' | 'web' | 'other'
const KIND: Record<string, Kind> = {
  read_file: 'read', list_files: 'search', glob: 'search', grep: 'search',
  edit_file: 'edit', write_file: 'edit', run_command: 'run', command_output: 'run', run_check: 'check',
  web_fetch: 'web', web_search: 'web',
}
const plural = (n: number, one: string, many: string): string => (n === 1 ? one : `${n} ${many}`)

/** "Read 3 files, searched twice, edited a file": what a run of steps did, in one line. */
export function groupSummary(steps: ToolStep[]): string {
  const counts = new Map<Kind, number>()
  for (const s of steps) {
    if (s.name === 'update_tasks') continue
    const k = s.output?.endsWith('nothing changed.') ? 'read' : KIND[s.name] ?? 'other'
    counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  const words: Record<Kind, (n: number) => string> = {
    read: n => `read ${plural(n, 'a file', 'files')}`,
    search: n => `searched ${n === 1 ? 'once' : n === 2 ? 'twice' : `${n} times`}`,
    edit: n => `edited ${plural(n, 'a file', 'files')}`,
    run: n => `ran ${plural(n, 'a command', 'commands')}`,
    check: n => `ran checks${n > 1 ? ` ${n} times` : ''}`,
    web: n => `used the web ${n === 1 ? 'once' : n === 2 ? 'twice' : `${n} times`}`,
    other: n => `used ${plural(n, 'a tool', 'tools')}`,
  }
  const text = [...counts].map(([k, n]) => words[k](n)).join(', ')
  return text ? text[0]!.toUpperCase() + text.slice(1) : 'Updated the task list'
}

/** Consecutive tool calls folded into one line, like a chapter: open it to see each step. */
export function ToolGroup({ steps, chatId, msgId, expanded = false }: { steps: ToolStep[]; chatId: string; msgId: string; expanded?: boolean }) {
  const [choice, setOpen] = useState<boolean | null>(null)
  const active = steps.some(s => s.output === undefined)
  const waiting = steps.some(s => s.approval && !s.approval.answer)
  const failed = steps.filter(s => s.output !== undefined && !s.ok).length
  const open = waiting || (choice ?? expanded)
  let add = 0
  let del = 0
  for (const s of steps) {
    const st = s.ok ? diffStats(s) : null
    if (st) { add += st.add; del += st.del }
  }
  const current = [...steps].reverse().find(s => s.output === undefined)
  return (
    <div className={`tool-group${open ? ' open' : ''}`}>
      <button type="button" className="group-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <Icon name={active ? 'bolt' : 'check'} size={14} className={active ? 'pulse-icon' : ''} />
        <span className={`group-title${active ? ' shimmer' : ''}`}>{groupSummary(steps)}</span>
        {(add > 0 || del > 0) && <span className="group-stats"><span className="stat-add">+{add}</span> <span className="stat-del">−{del}</span></span>}
        {failed > 0 && <span className="group-fail">{failed} failed</span>}
        {!open && current && <span className="group-now">{TOOLS[current.name]?.verb ?? current.name} {short(String(current.args[TOOLS[current.name]?.arg ?? ''] ?? ''), 48)}</span>}
        <Icon name="chevron" size={13} className={`tool-chev${open ? ' rot' : ''}`} />
      </button>
      {open && (
        <div className="group-body">
          {steps.map(s => <ToolRow key={s.id} step={s} chatId={chatId} msgId={msgId} expanded={expanded} />)}
        </div>
      )}
    </div>
  )
}
