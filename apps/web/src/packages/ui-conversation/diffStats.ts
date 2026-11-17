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

