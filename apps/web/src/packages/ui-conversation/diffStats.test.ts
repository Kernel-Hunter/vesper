import { describe, expect, it } from 'vitest'
import type { ToolStep } from '../store/store'
import { diffStats } from './diffStats'

const step = (over: Partial<ToolStep>): ToolStep => ({ id: '1', name: 'edit_file', args: {}, at: 0, n: 0, ok: true, output: 'ok', ...over })

describe('diffStats', () => {
  it('counts added and removed lines, not the file headers', () => {
    expect(diffStats(step({ diff: '--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+more' }))).toEqual({ add: 2, del: 1 })
  })
  it('falls back to the edit arguments', () => {
    expect(diffStats(step({ args: { old_string: 'a\nb', new_string: 'c' } }))).toEqual({ add: 1, del: 2 })
  })
  it('has nothing to say about other tools or writes that changed nothing', () => {
    expect(diffStats(step({ name: 'read_file' }))).toBeNull()
    expect(diffStats(step({ name: 'write_file', args: { content: 'x' }, output: 'x already has this content; nothing changed.' }))).toBeNull()
  })
})
