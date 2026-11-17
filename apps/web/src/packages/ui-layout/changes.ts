import type { Msg } from '../store/store'
import { diffStats } from '../ui-conversation/diffStats'

/** Successful file-tool edits in the retained conversation; undone answers are excluded. */
export function conversationChanges(messages: Msg[]): [string, string][] {
  const paths = new Map<string, string>()
  for (const message of messages) {
    if (message.undone) continue
    for (const step of message.tools) {
      if ((step.name === 'edit_file' || step.name === 'write_file') && step.ok && typeof step.args.path === 'string' &&
          !(step.name === 'write_file' && step.output?.endsWith(' already has this content; nothing changed.'))) {
        paths.set(step.args.path, step.name === 'write_file' ? 'Written' : 'Edited')
      }
    }
  }
  return [...paths.entries()].reverse()
}

/** Lines added and removed per file over the conversation, as far as the diffs tell. */
export function changeStats(messages: Msg[]): Map<string, { add: number; del: number }> {
  const out = new Map<string, { add: number; del: number }>()
  for (const message of messages) {
    if (message.undone) continue
    for (const step of message.tools) {
      const st = step.ok && typeof step.args.path === 'string' ? diffStats(step) : null
      if (!st) continue
      const prev = out.get(step.args.path as string) ?? { add: 0, del: 0 }
      out.set(step.args.path as string, { add: prev.add + st.add, del: prev.del + st.del })
    }
  }
  return out
}
