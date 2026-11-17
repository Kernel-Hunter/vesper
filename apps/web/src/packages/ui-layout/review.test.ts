import { describe, expect, it } from 'vitest'
import { diffRows } from '../ui-conversation/diffStats'

describe('diffRows', () => {
  it('numbers old and new lines across hunks and drops file headers', () => {
    const diff = ['--- a/app.py', '+++ b/app.py', '@@ -1,3 +1,3 @@', ' a = 1', '-b = 2', '+b = 20', ' c = 3',
      '@@ -10,2 +10,3 @@ def f():', ' x', '+y', ' z'].join('\n')
    const rows = diffRows(diff)
    expect(rows.map(r => r.kind)).toEqual(['hunk', 'ctx', 'del', 'add', 'ctx', 'hunk', 'ctx', 'add', 'ctx'])
    expect(rows[1]).toMatchObject({ text: 'a = 1', old: 1, now: 1 })
    expect(rows[2]).toMatchObject({ text: 'b = 2', old: 2 })
    expect(rows[3]).toMatchObject({ text: 'b = 20', now: 2 })
    expect(rows[4]).toMatchObject({ old: 3, now: 3 })
    expect(rows[7]).toMatchObject({ text: 'y', now: 11 })
    expect(rows[8]).toMatchObject({ old: 11, now: 12 })
  })

  it('handles an empty diff', () => {
    expect(diffRows('')).toEqual([])
  })
})
