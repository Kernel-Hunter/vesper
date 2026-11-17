import type { ToolStep } from '../store/store'

/** Lines added and removed by an edit, from whatever diff we have. */
export function diffStats(step: ToolStep): { add: number; del: number } | null {
  if (step.name !== 'edit_file' && step.name !== 'write_file') return null
  if (step.output?.endsWith('nothing changed.')) return null
  const diff = step.diff ?? step.approval?.diff ?? argsDiff(step)
  if (!diff) return null
  let add = 0
  let del = 0
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++') || line.startsWith('---')) continue
    if (line.startsWith('+')) add++
    else if (line.startsWith('-')) del++
  }
  return { add, del }
}

/** The change an edit asked for, when no diff came with it (a conversation loaded from history). */
export function argsDiff(step: ToolStep): string {
  const lines = (value: unknown, sign: string): string[] => (typeof value === 'string' && value ? value.split('\n').map(l => sign + l) : [])
  if (step.name === 'edit_file') return [...lines(step.args.old_string, '-'), ...lines(step.args.new_string, '+')].join('\n')
  if (step.name === 'write_file') return lines(step.args.content, '+').slice(0, 400).join('\n')
  return ''
}

export interface DiffRow { kind: 'hunk' | 'add' | 'del' | 'ctx'; text: string; old?: number; now?: number }

/** Unified diff text as rows with old and new line numbers (file headers dropped). */
export function diffRows(diff: string): DiffRow[] {
  const rows: DiffRow[] = []
  let old = 0
  let now = 0
  for (const line of diff.split('\n')) {
    if (line.startsWith('---') || line.startsWith('+++')) continue
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line)
    if (hunk) {
      old = Number(hunk[1])
      now = Number(hunk[2])
      rows.push({ kind: 'hunk', text: line })
    } else if (line.startsWith('+')) rows.push({ kind: 'add', text: line.slice(1), now: now++ })
    else if (line.startsWith('-')) rows.push({ kind: 'del', text: line.slice(1), old: old++ })
    else if (line.startsWith(' ')) rows.push({ kind: 'ctx', text: line.slice(1), old: old++, now: now++ })
  }
  return rows
}
