import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ContextReport, SavedSession, ServerState, StreamEvent } from '../connection/api'

describe('parallel conversations', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })

  it('keeps streams, folders, drafts, attachments and stop controls independent', async () => {
    vi.stubGlobal('location', { hash: '', pathname: '/', search: '' })
    vi.stubGlobal('document', { hidden: true, addEventListener: vi.fn() })
    vi.stubGlobal('window', { addEventListener: vi.fn() })
    vi.stubGlobal('requestAnimationFrame', (fn: () => void) => setTimeout(fn, 0))
    vi.stubGlobal('cancelAnimationFrame', (id: ReturnType<typeof setTimeout>) => clearTimeout(id))
    const { api } = await import('../connection/api')
    const store = await import('./store')
    vi.spyOn(api, 'state').mockResolvedValue({
      settings: { provider: 'fake', model: 'fake', workspace: '/first', agent_mode: 'ask', thinking: 'off' },
      providers: [{ id: 'fake', has_key: true }], home: '/home',
    } as ServerState)
    vi.spyOn(api, 'sessions').mockResolvedValue([
      { id: 'saved-first', title: 'first', cwd: '/first', updated_at: 1, source: 'desktop' },
      { id: 'saved-second', title: 'second', cwd: '/second', updated_at: 1, source: 'desktop' },
    ] as SavedSession[])
    const stop = vi.spyOn(api, 'stopChat').mockResolvedValue(true)
    const calls: { id: string; workspace?: string; event: (e: StreamEvent) => void; signal?: AbortSignal; finish: () => void }[] = []
    vi.spyOn(api, 'chat').mockImplementation((req, event, signal) => new Promise<void>(resolve => {
      calls.push({ id: req.chat_id, workspace: req.workspace, event, signal, finish: resolve })
    }))
    await store.refresh()

    const first = store.send('first question')
    const firstId = store.getState().currentId!
    store.setDraft('first draft')
    store.attachPath('/first/notes.txt')
    store.newChat()
    store.setDraft('second draft')
    store.attachPath('/second/notes.txt')
    const second = store.send('second question')
    const secondId = store.getState().currentId!
    expect(firstId).not.toBe(secondId)
    expect(store.getState().streamingIds).toEqual([firstId, secondId])
    expect(calls).toHaveLength(2)
    expect(calls[0]!.workspace).toBe('/first')

    calls[0]!.event({ event: 'session', data: { id: 'saved-first', cwd: '/first' } })
    calls[1]!.event({ event: 'session', data: { id: 'saved-second', cwd: '/second' } })
    calls[0]!.event({ event: 'cwd', data: { path: '/first/sub' } })
    expect(store.currentChat(store.getState())?.cwd).toBe('/second')
    store.selectChat(firstId)
    expect(store.currentChat(store.getState())?.cwd).toBe('/first/sub')
    expect(store.currentDraft(store.getState())).toBe('first draft')
    expect(store.currentAttachments(store.getState()).map(a => a.path)).toEqual(['/first/notes.txt'])
    store.stop()
    await Promise.resolve()
    expect(stop).toHaveBeenCalledWith(firstId)
    expect(calls[0]!.signal?.aborted).toBe(false)
    expect(calls[1]!.signal?.aborted).toBe(false)
    calls[0]!.finish()
    await first
    expect(store.getState().streamingIds).toEqual([secondId])
    store.selectChat(secondId)
    expect(store.currentDraft(store.getState())).toBe('') // sent attachments were cleared, the draft remains with the unsent composer
    calls[1]!.event({ event: 'token', data: { text: 'second answer' } })
    calls[1]!.finish()
    await second
    expect(store.currentChat(store.getState())?.messages.at(-1)?.text).toBe('second answer')
    expect(store.getState().streamingIds).toEqual([])
  })

  it('shows successful streamed file edits but not failed edits', async () => {
    vi.stubGlobal('location', { hash: '', pathname: '/', search: '' })
    vi.stubGlobal('document', { hidden: true, addEventListener: vi.fn() })
    vi.stubGlobal('window', { addEventListener: vi.fn() })
    vi.stubGlobal('requestAnimationFrame', (fn: () => void) => setTimeout(fn, 0))
    vi.stubGlobal('cancelAnimationFrame', (id: ReturnType<typeof setTimeout>) => clearTimeout(id))
    const { api } = await import('../connection/api')
    const store = await import('./store')
    const { conversationChanges } = await import('../ui-layout/changes')
    vi.spyOn(api, 'state').mockResolvedValue({
      settings: { provider: 'fake', model: 'fake', workspace: '/project', agent_mode: 'ask', thinking: 'off' },
      providers: [{ id: 'fake', has_key: true }], home: '/home',
    } as ServerState)
    vi.spyOn(api, 'sessions').mockResolvedValue([])
    vi.spyOn(api, 'chat').mockImplementation(async (_req, event) => {
      event({ event: 'tool_start', data: { id: 'edit-1', name: 'edit_file', args: { path: 'notes.txt' } } })
      event({ event: 'tool_end', data: { id: 'edit-1', name: 'edit_file', output: 'Edited notes.txt (1 change).', ok: true } })
      event({ event: 'tool_start', data: { id: 'edit-2', name: 'edit_file', args: { path: 'other.txt' } } })
      event({ event: 'tool_end', data: { id: 'edit-2', name: 'edit_file', output: 'Error: not found', ok: false } })
    })
    await store.refresh()
    await store.send('Edit notes')
    expect(conversationChanges(store.currentChat(store.getState())!.messages)).toEqual([['notes.txt', 'Edited']])
  })

  it('restores context and updates it after compaction without affecting another chat', async () => {
    vi.stubGlobal('location', { hash: '', pathname: '/', search: '' })
    vi.stubGlobal('document', { hidden: true, addEventListener: vi.fn() })
    vi.stubGlobal('window', { addEventListener: vi.fn() })
    const { api } = await import('../connection/api')
    const store = await import('./store')
    const first = { id: 'aaaa', title: 'first', cwd: '/first', updated_at: 1 } as SavedSession
    const second = { id: 'bbbb', title: 'second', cwd: '/second', updated_at: 1 } as SavedSession
    const report = (used: number): ContextReport => ({ used, window: 1_000_000, window_source: 'documented', provider: 'anthropic', model: 'claude-sonnet-5-5', history: used, history_budget: 600_000, breakdown: { instructions: 0, tools: 0, conversation: used, tool_results: 0, summary: 0 } })
    vi.spyOn(api, 'session').mockImplementation(async id => ({ ...(id === first.id ? first : second), turns: [], context_report: report(id === first.id ? 100 : 200) }))
    vi.spyOn(api, 'compact').mockResolvedValue({ compacted: true, before: 200, context: 50, context_report: report(50) })
    await store.openSession(first.id)
    await store.openSession(second.id)
    expect(store.currentChat(store.getState())?.contextReport?.used).toBe(200)
    await store.compactChat()
    expect(store.currentChat(store.getState())?.contextReport?.used).toBe(50)
    store.selectChat(first.id)
    expect(store.currentChat(store.getState())?.contextReport?.used).toBe(100)
  })

  it('reflects another window’s work and reloads its finished conversation without losing the draft', async () => {
    vi.stubGlobal('location', { hash: '', pathname: '/', search: '' })
    vi.stubGlobal('document', { hidden: true, addEventListener: vi.fn() })
    vi.stubGlobal('window', { addEventListener: vi.fn() })
    const { api } = await import('../connection/api')
    const store = await import('./store')
    const row = { id: 'abcdef123456', title: 'chat', cwd: '/project', updated_at: 1, source: 'desktop' } as SavedSession
    let current = { ...row, working: false }
    vi.spyOn(api, 'sessions').mockImplementation(async () => [current])
    const session = vi.spyOn(api, 'session').mockResolvedValue({ ...row, turns: [{ role: 'user', text: 'first' }] })
    await store.loadHistory()
    await store.openSession(row.id)
    store.setDraft('keep this draft')
    current = { ...row, working: true }
    await store.loadHistory()
    expect(store.currentRemoteBusy(store.getState())).toBe(true)
    expect(store.currentBusy(store.getState())).toBe(true)
    current = { ...row, updated_at: 2, working: false }
    session.mockResolvedValue({ ...current, turns: [{ role: 'user', text: 'first' }, { role: 'assistant', parts: [{ type: 'text', text: 'new answer' }] }] })
    await store.loadHistory()
    await vi.waitFor(() => expect(store.currentChat(store.getState())?.messages.at(-1)?.text).toBe('new answer'))
    expect(store.currentRemoteBusy(store.getState())).toBe(false)
    expect(store.currentDraft(store.getState())).toBe('keep this draft')
  })
})
