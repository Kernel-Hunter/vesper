import { describe, expect, it } from 'vitest'
import { blankMsg } from '../store/history'
import { conversationChanges } from './changes'

describe('conversation changes', () => {
  it('omits unchanged writes, failed edits and undone answers', () => {
    const msg = blankMsg('bot')
    msg.tools = [
      { id: '1', name: 'write_file', args: { path: 'same.txt' }, at: 0, n: 0, ok: true, output: 'same.txt already has this content; nothing changed.' },
      { id: '2', name: 'edit_file', args: { path: 'failed.txt' }, at: 0, n: 1, ok: false },
      { id: '3', name: 'write_file', args: { path: 'new.txt' }, at: 0, n: 2, ok: true, output: 'Created new.txt (1 lines).' },
    ]
    expect(conversationChanges([msg])).toEqual([['new.txt', 'Written']])
    expect(conversationChanges([{ ...msg, undone: true }])).toEqual([])
  })
})
