import { useSyncExternalStore } from 'react'
import {
  api, ApiError, type AgentMode, type ApprovalAnswer, type ApprovalRequest, type ImageUpload, type SavedSession,
  type ContextReport, type GitAction, type ServerState, type SidebarChanges, type StreamEvent, type Task, type Usage,
} from '../connection/api'
import { APPEARANCE_KEY, applyAppearance, loadAppearance, saveAppearance, type Appearance } from '../ui-theme/appearance'
import { blankMsg, MAX_TOOL_OUTPUT, turnsToMessages, uid } from './history'

/** One tool call the agent made, drawn inline at text offset `at`. */
export interface ToolStep {
  id: string
  name: string
  args: Record<string, unknown>
  at: number
  /** Order among the tool calls and thoughts of one answer (several can share one text offset). */
  n: number
  output?: string
  ok?: boolean
  ms?: number
  /** The change an edit made, when it ran without an approval card. */
  diff?: string
  /** Output of a command that is still running. */
  live?: string
  approval?: ApprovalRequest & { answer?: ApprovalAnswer; sending?: boolean }
}

/** A stretch of the model's thinking, drawn inline at text offset `at`. */
export interface Thought {
  at: number
  n: number
  text: string
}

export interface Msg {
  id: string
  role: 'user' | 'bot'
  text: string
  error: string | null
  tools: ToolStep[]
  thoughts: Thought[]
  /** Files, folders and images sent with a question. */
  attachments?: string[]
  /** The engine's id for this question ("vesper-turn-N"): rewinding names it exactly. */
  turnId?: string
  usage?: Usage
  /** The agent's task list, as last updated in this answer. */
  tasks?: Task[]
  /** Written in plan mode: the answer is a plan waiting for a yes. */
  plan?: boolean
  /** The files this answer changed were put back. */
  undone?: boolean
  /** Auto-commit after this answer (/git auto on): the commit, or why there was none. */
  commit?: { sha: string; message: string; files: string[]; skipped: string[]; error: string }
  /** The tool call the model is writing right now ("Edit, 340 characters"). */
  draft?: { name: string; chars: number }
  streaming?: boolean
}

/** An open conversation. Saved ones live in the session database the terminal app shares. */
export interface Chat {
  id: string
  title: string
  updated: number
  messages: Msg[]
  /** Working folder belongs to this conversation, not other open chats. */
  cwd: string
  /** Its row in the session database, once the engine has saved it. */
  sessionId?: string
  /** Database revision of the messages shown here, in seconds. */
  syncedAt?: number
  contextReport?: ContextReport
  lastUsage?: Usage
  /** The last answer never finished: Vesper closed or crashed while it ran. */
  cutOff?: boolean
}

/** Something to send with the next message: a file or folder by path, or a pasted image. */
export interface Attachment {
  key: string
  name: string
  kind: 'file' | 'folder' | 'image'
  path?: string
  image?: ImageUpload
  /** A small preview for images. */
  preview?: string
}

export type SettingsTab = 'agent' | 'models' | 'tools' | 'appearance'

export interface Toast {
  id: number
  text: string
  kind: 'info' | 'error'
}

export interface State {
  server: ServerState | null
  connected: boolean
  /** Conversations opened in this window, newest first. */
  chats: Chat[]
  currentId: string | null
  newDraftId: string
  streamingIds: string[]
  appearance: Appearance
  settings: { open: boolean; tab: SettingsTab; focusProvider: string | null }
  sidebar: boolean
  toasts: Toast[]
  /** Every saved conversation (terminal and desktop), newest first. */
  history: SavedSession[]
  historyLoaded: boolean
  /** Transcript view: thinking, commands, edits and output are shown in full. */
  transcript: boolean
  /** Drafts and attachments stay with their conversation while switching. */
  drafts: Record<string, string>
  /** Messages typed while a chat was answering; sent one by one when it finishes. */
  queued: Record<string, string[]>
  attachments: Record<string, Attachment[]>
}

/* What survives closing the app: the open conversation and every unsent draft. */
const RESUME_KEY = 'vesper.resume'
interface Resume { session?: string; drafts?: Record<string, string> }
const readResume = (): Resume => {
  try { const v: unknown = JSON.parse(localStorage.getItem(RESUME_KEY) || '{}'); return v && typeof v === 'object' ? v as Resume : {} } catch { return {} }
}
let resume = readResume()
let resumeTimer: ReturnType<typeof setTimeout> | undefined
const saveResume = (patch: Resume): void => {
  resume = { ...resume, ...patch }
  clearTimeout(resumeTimer)
  resumeTimer = setTimeout(() => { try { localStorage.setItem(RESUME_KEY, JSON.stringify(resume)) } catch { /* private mode */ } }, 250)
}
/** The conversation that was open when the app last closed (if any). */
export const lastSession = (): string => (typeof resume.session === 'string' ? resume.session : '')

const firstDraftId = uid()

let state: State = {
  server: null,
  connected: true,
  chats: [],
  currentId: null,
  newDraftId: firstDraftId,
  streamingIds: [],
  appearance: loadAppearance(),
  settings: { open: false, tab: 'models', focusProvider: null },
  sidebar: true,
  toasts: [],
  history: [],
  historyLoaded: false,
  transcript: (() => { try { return localStorage.getItem('vesper.transcript') === '1' } catch { return false } })(),
  drafts: typeof resume.drafts?.new === 'string' ? { [firstDraftId]: resume.drafts.new } : {},
  queued: {},
  attachments: {},
}
// Chats used to be kept in this window's storage; the session database holds them now.
try { for (const k of ['vesper.chats', 'pagewise.chats', 'raglab.chats']) localStorage.removeItem(k) } catch { /* private mode */ }

const listeners = new Set<() => void>()
function set(patch: Partial<State> | ((s: State) => Partial<State>)): void {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) }
  listeners.forEach(l => l())
}
const subscribe = (l: () => void): (() => void) => { listeners.add(l); return () => listeners.delete(l) }

export function useStore<T>(select: (s: State) => T): T {
  return useSyncExternalStore(subscribe, () => select(state))
}
export const getState = (): State => state
const errorText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

// ---------------- toasts ----------------
let toastId = 0
export function toast(text: string, kind: Toast['kind'] = 'info'): void {
  const id = ++toastId
  set(s => ({ toasts: [...s.toasts.filter(t => t.text !== text), { id, text, kind }].slice(-4) }))
  setTimeout(() => set(s => ({ toasts: s.toasts.filter(t => t.id !== id) })), kind === 'error' ? 7000 : 3200)
}

// ---------------- server state ----------------
let pollTimer: ReturnType<typeof setTimeout> | undefined

/** Reads settings, keys and models from the engine. It re-reads the files the terminal app writes,
 *  so a key or model chosen there shows up here within a few seconds. */
export async function refresh(): Promise<void> {
  clearTimeout(pollTimer)
  try {
    const server = await api.state()
    set({ server, connected: true })
    if (!document.hidden) void loadHistory() // conversations the terminal app added, renamed or deleted
  } catch (e) {
    set({ connected: false })
    if (e instanceof ApiError && e.status === 401) {
      toast('Session expired. Restart Vesper.', 'error')
      return
    }
  }
  const busy = !state.server || !state.connected
  if (!document.hidden || busy) pollTimer = setTimeout(() => void refresh(), busy ? 1000 : 5000)
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) { void refresh(); void loadHistory() } })
window.addEventListener('focus', () => { void refresh(); void loadHistory() })

export async function saveSettings(values: Record<string, unknown>, okText?: string): Promise<boolean> {
  try {
    const server = await api.settings(values)
    set({ server })
    if (okText) toast(okText)
    return true
  } catch (e) {
    toast(errorText(e), 'error')
    return false
  }
}

// ---------------- UI ----------------
export function openSettings(tab: SettingsTab = 'models', focusProvider: string | null = null): void {
  set({ settings: { open: true, tab, focusProvider } })
}
export const closeSettings = (): void => set(s => ({ settings: { ...s.settings, open: false } }))
export const setSettingsTab = (tab: SettingsTab): void => set(s => ({ settings: { ...s.settings, tab, focusProvider: null } }))
export const toggleSidebar = (): void => set(s => ({ sidebar: !s.sidebar }))
export function toggleTranscript(): void {
  set(s => ({ transcript: !s.transcript }))
  try { localStorage.setItem('vesper.transcript', state.transcript ? '1' : '0') } catch { /* private mode */ }
  toast(state.transcript ? 'Transcript view: thinking, commands and output in full' : 'Transcript view off')
}

export function setAppearance(patch: Partial<Appearance>): void {
  const appearance = { ...state.appearance, ...patch }
  saveAppearance(appearance)
  applyAppearance(appearance)
  set({ appearance })
}
window.addEventListener('storage', (event: StorageEvent) => {
  if (event.key !== APPEARANCE_KEY) return
  const appearance = loadAppearance()
  applyAppearance(appearance)
  set({ appearance })
})
if (typeof matchMedia === 'function') {
  const reapply = (): void => { const appearance = { ...state.appearance }; applyAppearance(appearance); set({ appearance }) }
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', reapply)
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', reapply)
}

// ---------------- chats ----------------
export const currentChat = (s: State): Chat | null => s.chats.find(c => c.id === s.currentId) ?? null
export const currentRemoteBusy = (s: State): boolean => {
  const chat = currentChat(s)
  return !!chat?.sessionId && !s.streamingIds.includes(chat.id) && !!s.history.find(h => h.id === chat.sessionId)?.working
}
export const currentBusy = (s: State): boolean => (!!s.currentId && s.streamingIds.includes(s.currentId)) || currentRemoteBusy(s)
const draftKey = (s: State): string => s.currentId ?? s.newDraftId
const draftAliases = new Map<string, string>()
export const currentDraft = (s: State): string => s.drafts[draftKey(s)] ?? ''
const NO_ATTACHMENTS: Attachment[] = []
export const currentAttachments = (s: State): Attachment[] => s.attachments[draftKey(s)] ?? NO_ATTACHMENTS
export function setDraft(value: string | ((old: string) => string), target = draftKey(state)): void {
  set(s => {
    const key = target
    return { drafts: { ...s.drafts, [key]: typeof value === 'function' ? value(s.drafts[key] ?? '') : value } }
  })
  // Kept by conversation (its saved id), so the text is still there after a restart.
  const chat = state.chats.find(c => c.id === target)
  const where = target === state.newDraftId ? 'new' : chat?.sessionId ?? (chat ? '' : target)
  if (where) {
    const drafts = { ...(resume.drafts ?? {}) }
    const text = state.drafts[target] ?? ''
    if (text) drafts[where] = text.slice(0, 50_000)
    else delete drafts[where]
    const keys = Object.keys(drafts)
    for (const k of keys.slice(0, Math.max(0, keys.length - 50))) delete drafts[k] // keep the 50 latest
    saveResume({ drafts })
  }
}

function rememberOpen(): void {
  saveResume({ session: currentChat(state)?.sessionId ?? '' })
}

let selection = 0
export function selectChat(id: string): void {
  selection++
  set({ currentId: id })
  rememberOpen()
  const sid = state.chats.find(c => c.id === id)?.sessionId
  if (sid && state.history.find(h => h.id === sid)?.unread) void updateSidebar(sid, { unread: false })
}

export function newChat(): void {
  selection++
  set({ currentId: null, newDraftId: uid() })
  saveResume({ session: '' })
}

/** After a restart: open the conversation that was open when Vesper closed, quietly. */
export async function resumeLast(): Promise<void> {
  const id = lastSession()
  if (!/^[0-9a-f]{4,32}$/.test(id)) return
  for (let i = 0; i < 40 && !state.server; i++) await new Promise(r => setTimeout(r, 500)) // the engine is starting
  if (state.currentId || currentDraft(state)) return // the user already started something else
  try { await api.session(id) } catch { saveResume({ session: '' }); return } // deleted since
  await openSession(id)
}

/** Opens a conversation from the session database (or the copy already open). */
export async function openSession(id: string): Promise<void> {
  const request = ++selection
  if (state.history.find(h => h.id === id)?.unread) void updateSidebar(id, { unread: false })
  const open = state.chats.find(c => c.sessionId === id)
  if (open) { set({ currentId: open.id }); rememberOpen(); return }
  try {
    const saved = await api.session(id)
    if (request !== selection) return
    const chat: Chat = {
      id: saved.id, title: saved.title || 'Untitled', updated: saved.updated_at * 1000,
      messages: turnsToMessages(saved.turns), sessionId: saved.id, cwd: saved.cwd, syncedAt: saved.updated_at,
      contextReport: saved.context_report, lastUsage: saved.last_usage ?? undefined, cutOff: !!saved.cut_off,
    }
    const draft = resume.drafts?.[saved.id]
    set(s => ({ chats: [chat, ...s.chats.filter(c => c.id !== chat.id)], currentId: chat.id,
      drafts: draft && !s.drafts[chat.id] ? { ...s.drafts, [chat.id]: draft } : s.drafts }))
    rememberOpen()
  } catch (e) {
    toast(errorText(e), 'error')
    void loadHistory()
  }
}

/** Deletes a conversation everywhere: this window, the engine and the session database. */
export async function deleteSession(id: string): Promise<void> {
  const chat = state.chats.find(c => c.sessionId === id || c.id === id)
  if (chat && state.streamingIds.includes(chat.id)) {
    toast('Stop this conversation and wait for it to finish before deleting it.', 'error')
    return
  }
  try {
    if (chat?.sessionId || !chat) await api.deleteSession(chat?.sessionId ?? id)
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) { // already gone is fine
      toast(`Could not delete: ${errorText(e)}`, 'error')
      return
    }
  }
  if (chat) await api.forgetChat(chat.id).catch(() => undefined)
  set(s => {
    const chats = s.chats.filter(c => c !== chat)
    return {
      chats,
      history: s.history.filter(h => h.id !== id && h.id !== chat?.sessionId),
      currentId: s.currentId === chat?.id ? null : s.currentId,
    }
  })
  toast('Conversation deleted')
}

/** Save a conversation as a Markdown or HTML file (the browser's or Electron's save flow). */
export async function exportSession(id: string, format: 'md' | 'html'): Promise<void> {
  try {
    const { name, text } = await api.exportSession(id, format)
    const url = URL.createObjectURL(new Blob([text], { type: format === 'html' ? 'text/html' : 'text/markdown' }))
    const link = document.createElement('a')
    link.href = url
    link.download = name
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
  } catch (e) {
    toast(`Could not export: ${errorText(e)}`, 'error')
  }
}

export async function renameSession(id: string, title: string): Promise<void> {
  const clean = title.trim().replace(/\s+/g, ' ').slice(0, 80)
  if (!clean) return
  const chat = state.chats.find(c => c.sessionId === id || c.id === id)
  const sessionId = chat?.sessionId ?? (chat ? undefined : id)
  set(s => ({
    chats: s.chats.map(c => (c === chat ? { ...c, title: clean } : c)),
    history: s.history.map(h => (h.id === sessionId ? { ...h, title: clean } : h)),
  }))
  if (!sessionId) return // not saved yet: the title goes with the first answer
  try {
    await api.renameSession(sessionId, clean)
  } catch (e) {
    toast(`Could not rename: ${errorText(e)}`, 'error')
    void loadHistory()
  }
}

function patchMessage(chatId: string, msgId: string, patch: (m: Msg) => Partial<Msg>): void {
  set(s => ({
    chats: s.chats.map(c => (c.id !== chatId ? c : {
      ...c,
      updated: Date.now(),
      messages: c.messages.map(m => (m.id === msgId ? { ...m, ...patch(m) } : m)),
    })),
  }))
}

const aborts = new Map<string, AbortController>()

/** Stop only the selected conversation; leave other streams and approvals alone. */
export function stop(): void {
  const id = state.currentId
  if (!id || !state.streamingIds.includes(id)) return
  const controller = aborts.get(id)
  void api.stopChat(id).then(stopped => {
    if (!stopped && aborts.get(id) === controller) controller?.abort()
  }, () => { if (aborts.get(id) === controller) controller?.abort() })
}

const NO_QUEUE: string[] = []
export const currentQueue = (s: State): string[] => (s.currentId ? s.queued[s.currentId] ?? NO_QUEUE : NO_QUEUE)

/** Keeps a message typed while the open chat is answering; it goes out when the answer ends. */
/** Typed while the chat works: waits in the queue and also asks the engine to add it to the
 *  running answer. When the engine takes it, a 'steer' event removes it from the queue; if the
 *  answer ends first, it is sent as the next message like any queued one. */
export function steerMessage(text: string): void {
  const id = state.currentId
  if (!id) return
  queueMessage(text)
  void api.steerChat(id, text.trim()).catch(() => undefined)
}

export function queueMessage(text: string): void {
  const id = state.currentId
  if (!id || !text.trim()) return
  set(s => ({ queued: { ...s.queued, [id]: [...(s.queued[id] ?? []), text.trim()] } }))
}

export function unqueue(chatId: string, index: number): string | undefined {
  const item = state.queued[chatId]?.[index]
  set(s => ({ queued: { ...s.queued, [chatId]: (s.queued[chatId] ?? []).filter((_, i) => i !== index) } }))
  return item
}

/** A system notification when the window is in the background (answer ready, approval waiting). */
export function notify(title: string, body: string): void {
  try {
    if (typeof Notification === 'undefined' || (!document.hidden && document.hasFocus())) return
    if (Notification.permission === 'granted') {
      const n = new Notification(title, { body: body.slice(0, 160), silent: false })
      n.onclick = () => { window.focus(); n.close() }
    } else if (Notification.permission === 'default') {
      void Notification.requestPermission()
    }
  } catch { /* notifications are a nicety */ }
}

/** True when a message can be sent now; otherwise explains why with a toast. */
function readyToSend(chatId?: string): boolean {
  const s = state
  if (chatId ? s.streamingIds.includes(chatId) : currentBusy(s)) return false
  if (!s.server) { toast('Still connecting to the engine…', 'error'); return false }
  const p = s.server.providers.find(x => x.id === s.server?.settings.provider)
  if (p && p.needs_key && !p.has_key) {
    openSettings('models', p.id)
    toast(`Add your ${p.label} key first.`, 'error')
    return false
  }
  return true
}

export async function send(text: string, opts: { retry?: boolean; chatId?: string } = {}): Promise<void> {
  const question = text.trim()
  const attached = opts.retry || opts.chatId ? [] : currentAttachments(state)
  if (!question && !attached.length) return
  if (!readyToSend(opts.chatId)) return

  const attachmentKey = opts.chatId ?? draftKey(state)
  let chatId = opts.chatId ?? state.currentId
  if (!chatId || !state.chats.some(c => c.id === chatId)) {
    chatId = uid()
    draftAliases.set(attachmentKey, chatId)
    const chat: Chat = { id: chatId, title: (question || attached[0]!.name).slice(0, 60), updated: Date.now(), messages: [],
      cwd: state.server?.settings.workspace || state.server?.home || '' }
    set(st => ({ chats: [chat, ...st.chats], currentId: chatId }))
  }
  const user: Msg = { ...blankMsg('user'), text: question, attachments: attached.length ? attached.map(a => a.name) : undefined }
  const settings = state.server?.settings
  const bot: Msg = { ...blankMsg('bot'), streaming: true, plan: settings?.agent_mode === 'plan' || undefined }
  const sessionId = state.chats.find(c => c.id === chatId)?.sessionId
  const cid = chatId
  let botId = bot.id // a message steered into the answer starts a new answer bubble
  set(st => ({
    streamingIds: [...st.streamingIds, cid],
    attachments: opts.retry ? st.attachments : { ...st.attachments, [attachmentKey]: [] },
    chats: st.chats.map(c => (c.id === cid ? { ...c, updated: Date.now(), cutOff: false, messages: [...c.messages, user, bot] } : c))
      .sort((a, b) => b.updated - a.updated),
  }))

  // Tokens arrive fast; batch them into one render per animation frame.
  let pending = ''
  let frame = 0
  let stopped = false // the engine ended this answer because Stop was pressed
  const flush = (): void => {
    frame = 0
    if (!pending) return
    const add = pending
    pending = ''
    patchMessage(cid, botId, m => ({ text: m.text + add }))
  }

  const controller = new AbortController()
  aborts.set(cid, controller)
  try {
    const patchTool = (match: (t: ToolStep) => boolean, patch: (t: ToolStep) => Partial<ToolStep>): void =>
      patchMessage(cid, botId, m => {
        // Newest match first: the same tool can run several times in one answer.
        const i = m.tools.map(match).lastIndexOf(true)
        if (i < 0) return {}
        const tools = m.tools.slice()
        tools[i] = { ...tools[i]!, ...patch(tools[i]!) }
        return { tools }
      })
    let thinking = false // the last thing that arrived was thinking: more of it joins the same block
    await api.chat({
      question: question || 'Look at what I attached.',
      chat_id: cid,
      session_id: sessionId,
      workspace: state.chats.find(c => c.id === cid)?.cwd || undefined,
      retry: opts.retry || undefined,
      attachments: attached.flatMap(a => (a.path ? [a.path] : [])),
      images: attached.flatMap(a => (a.image ? [a.image] : [])),
    }, (e: StreamEvent) => {
      if (e.event !== 'reasoning' && e.event !== 'output') thinking = false
      switch (e.event) {
        case 'session':
          set(st => ({ chats: st.chats.map(c => (c.id === cid ? { ...c, sessionId: e.data.id, cwd: e.data.cwd, cutOff: false } : c)) }))
          if (cid === state.currentId) rememberOpen()
          break
        case 'turn':
          patchMessage(cid, user.id, () => ({ turnId: e.data.id }))
          break
        case 'saved':
          set(st => ({ chats: st.chats.map(c => c.id === cid && c.sessionId === e.data.id ? { ...c, syncedAt: e.data.updated_at } : c) }))
          break
        case 'attached':
          if (e.data.problems.length) toast(`Not attached: ${e.data.problems.join('; ')}`, 'error')
          break
        case 'reasoning': {
          flush()
          const more = thinking
          thinking = true
          patchMessage(cid, botId, m => {
            const last = m.thoughts[m.thoughts.length - 1]
            if (more && last) return { thoughts: [...m.thoughts.slice(0, -1), { ...last, text: last.text + e.data.text }] }
            return { thoughts: [...m.thoughts, { at: m.text.length, n: m.tools.length + m.thoughts.length, text: e.data.text }] }
          })
          break
        }
        case 'tool_draft':
          patchMessage(cid, botId, () => ({ draft: e.data }))
          break
        case 'tasks':
          patchMessage(cid, botId, () => ({ tasks: e.data.items }))
          break
        case 'commit':
          patchMessage(cid, botId, () => ({ commit: e.data }))
          window.dispatchEvent(new Event('vesper:git-changed'))
          break
        case 'tool_start':
          flush()
          if (e.data.name === 'update_tasks') { // drawn as the task list, not as a tool row
            patchMessage(cid, botId, () => ({ draft: undefined }))
            break
          }
          patchMessage(cid, botId, m => ({
            draft: undefined,
            tools: [...m.tools, {
              id: e.data.id ?? uid(), name: e.data.name, args: e.data.args ?? {}, at: m.text.length,
              n: m.tools.length + m.thoughts.length,
            }],
          }))
          break
        case 'tool_end':
          patchTool(t => (e.data.id ? t.id === e.data.id : t.name === e.data.name && t.output === undefined),
            () => ({ output: e.data.output, ok: e.data.ok, ms: e.data.ms ?? undefined, live: undefined }))
          break
        case 'output':
          { // "helper 2: grep foo" is a helper's step; anything else is a running command's output
            const helper = e.data.line.startsWith('helper ')
            patchTool(t => (helper ? t.name === 'subagent' : t.name === 'run_command' || t.name === 'run_check') && t.output === undefined,
              t => ({ live: ((t.live ?? '') + e.data.line + '\n').slice(-MAX_TOOL_OUTPUT) }))
          }
          break
        case 'auto':
          if (e.data.diff) patchTool(t => t.name === e.data.tool && t.output === undefined, () => ({ diff: e.data.diff }))
          break
        case 'title':
          set(st => ({
            chats: st.chats.map(c => (c.id === cid ? { ...c, title: e.data.title } : c)),
            history: st.history.map(h => (h.id === st.chats.find(c => c.id === cid)?.sessionId ? { ...h, title: e.data.title } : h)),
          }))
          break
        case 'usage':
          patchMessage(cid, botId, () => ({ usage: e.data }))
          set(st => ({ chats: st.chats.map(c => c.id === cid ? { ...c, contextReport: e.data.context_report, lastUsage: e.data } : c) }))
          break
        case 'approval':
          notify('Vesper is waiting for you', `Allow ${e.data.kind === 'run' ? e.data.command ?? 'a command' : e.data.kind === 'mcp' ? e.data.mcp_tool ?? 'a tool' : e.data.path ?? e.data.url ?? 'a change'}?`)
          patchTool(t => t.name === e.data.tool && t.output === undefined && !t.approval, () => ({ approval: e.data }))
          break
        case 'approval_done':
          patchTool(t => t.approval?.id === e.data.id, t => ({ approval: { ...t.approval!, answer: e.data.answer, sending: false } }))
          if (e.data.timeout) toast('No answer for 10 minutes, so the action was skipped.', 'error')
          break
        case 'token':
          pending += e.data.text
          if (!frame) frame = requestAnimationFrame(flush)
          break
        case 'error':
          patchMessage(cid, botId, () => ({ error: e.data.message }))
          break
        case 'cwd': // update only the conversation that moved
          set(st => ({ chats: st.chats.map(c => c.id === cid ? { ...c, cwd: e.data.path } : c) }))
          break
        case 'notice':
          toast(e.data.text)
          break
        case 'stopped':
          stopped = true
          break
        case 'steer': { // a message typed during the answer joined it: show it, then continue in a new bubble
          flush()
          const asked: Msg = { ...blankMsg('user'), text: e.data.texts.join('\n\n'), turnId: e.data.id }
          const next: Msg = { ...blankMsg('bot'), streaming: true }
          const done = botId
          botId = next.id
          set(st => {
            let queue = st.queued[cid] ?? []
            for (const t of e.data.texts) { const i = queue.indexOf(t); if (i >= 0) queue = queue.filter((_, j) => j !== i) }
            return {
              queued: { ...st.queued, [cid]: queue },
              chats: st.chats.map(c => (c.id === cid ? { ...c, messages: [...c.messages.map(m => (m.id === done ? { ...m, streaming: false, draft: undefined } : m)), asked, next] } : c)),
            }
          })
          break
        }
        default:
          break
      }
    }, controller.signal)
  } catch (e) {
    if (!(e instanceof DOMException && e.name === 'AbortError')) {
      patchMessage(cid, botId, () => ({ error: errorText(e) }))
    }
  } finally {
    if (frame) cancelAnimationFrame(frame)
    flush()
    patchMessage(cid, botId, m => ({
      streaming: false,
      draft: undefined,
      text: stopped && m.text ? `${m.text}\n\n_Stopped._` : m.text || (m.error || m.tools.length ? '' : '_Stopped._'),
      tools: m.tools.map(t => ({
        ...t,
        live: undefined,
        output: t.output ?? (t.live ? `${t.live}\n(stopped)` : t.output),
        // Anything still waiting was refused by the engine when the stream closed.
        approval: t.approval && !t.approval.answer ? { ...t.approval, answer: 'no' as const } : t.approval,
      })),
    }))
    if (aborts.get(cid) === controller) aborts.delete(cid)
    set(st => ({ streamingIds: st.streamingIds.filter(id => id !== cid) }))
    const finished = state.chats.find(c => c.id === cid)
    if (finished?.sessionId && state.currentId !== cid) void updateSidebar(finished.sessionId, { unread: true })
    void loadHistory()
    const next = state.queued[cid]?.[0]
    if (next !== undefined && !stopped) { // typed while it worked: send it now, in the same chat
      unqueue(cid, 0)
      void send(next, { chatId: cid })
    } else if (finished) {
      notify(stopped ? 'Vesper stopped' : 'Vesper is done', finished.title || 'Your answer is ready.')
    }
  }
}

/** Asks the last question again. The engine forgets the old answer first. */
export async function retry(chatId: string, botId: string): Promise<void> {
  const chat = state.chats.find(c => c.id === chatId)
  if (!chat || state.streamingIds.includes(chat.id)) return
  const i = chat.messages.findIndex(m => m.id === botId)
  const question = i > 0 ? chat.messages[i - 1] : undefined
  if (!question || question.role !== 'user' || i !== chat.messages.length - 1) return
  if (!readyToSend()) return // keep the old answer if we cannot ask again
  set(s => ({
    currentId: chatId,
    chats: s.chats.map(c => (c.id === chatId ? { ...c, messages: c.messages.slice(0, i - 1) } : c)),
  }))
  await send(question.text, { retry: true })
}

// ---------------- saved sessions ----------------
/** Persist organization independently of the conversation's message timestamps. */
export async function updateSidebar(id: string, changes: SidebarChanges): Promise<void> {
  try {
    await api.sidebarSession(id, changes)
    set(s => ({ history: s.history.map(h => h.id !== id ? h : {
      ...h,
      ...(changes.pinned !== undefined ? { pinned: Number(changes.pinned) } : {}),
      ...(changes.archived !== undefined ? { archived: Number(changes.archived) } : {}),
      ...(changes.unread !== undefined ? { unread: Number(changes.unread) } : {}),
      ...(changes.completed !== undefined ? { completed: Number(changes.completed) } : {}),
      ...(changes.section !== undefined ? { section: changes.section } : {}),
      ...(changes.position !== undefined ? { position: changes.position } : {}),
    }) }))
  } catch (e) { toast(e instanceof Error ? e.message : String(e), 'error') }
}

export async function forkSession(id: string): Promise<void> {
  try {
    const fork = await api.forkSession(id)
    await loadHistory()
    await openSession(fork)
    toast('Fork created. Both conversations use the same folder.', 'info')
  } catch (e) { toast(e instanceof Error ? e.message : String(e), 'error') }
}

/** Every conversation in the session database: the sidebar's list. */
const HISTORY_LIMIT = 200 // matches api.sessions
const syncing = new Set<string>()
async function syncConversation(id: string): Promise<void> {
  if (syncing.has(id)) return
  syncing.add(id)
  try {
    const saved = await api.session(id)
    set(s => ({ chats: s.chats.map(chat => {
      if (chat.sessionId !== id || s.streamingIds.includes(chat.id) ||
          chat.syncedAt === undefined || saved.updated_at <= chat.syncedAt) return chat
      const incoming = turnsToMessages(saved.turns)
      // Keep live-only details (such as usage and undo controls) for unchanged messages.
      const messages = incoming.map((m, i) => {
        const previous = chat.messages[i]
        return previous && previous.role === m.role && previous.text === m.text ? previous : m
      })
      return { ...chat, title: saved.title || 'Untitled', cwd: saved.cwd, updated: saved.updated_at * 1000,
        syncedAt: saved.updated_at, messages, contextReport: saved.context_report, lastUsage: saved.last_usage ?? undefined }
    }) }))
  } catch { /* the next history poll retries */ } finally { syncing.delete(id) }
}
export async function loadHistory(): Promise<void> {
  try {
    const history = await api.sessions()
    set(s => {
      // A chat open here whose conversation was deleted elsewhere (the terminal) goes too. Only when the
      // list is complete: a full page may leave out an old conversation that still exists.
      const ids = new Set(history.map(h => h.id))
      const complete = history.length < HISTORY_LIMIT
      const chats = s.chats.filter(c => !complete || !c.sessionId || ids.has(c.sessionId) || s.streamingIds.includes(c.id))
      return {
        history,
        historyLoaded: true,
        chats,
        currentId: chats.some(c => c.id === s.currentId) ? s.currentId : null,
      }
    })
    for (const chat of state.chats) {
      const row = history.find(h => h.id === chat.sessionId)
      if (row && !row.working && chat.syncedAt !== undefined && row.updated_at > chat.syncedAt && !state.streamingIds.includes(chat.id)) {
        void syncConversation(row.id)
      }
    }
  } catch { /* the engine is not up yet; refresh() retries */ }
}

// ---------------- attachments ----------------
interface DesktopBridge {
  pickFolder?: () => Promise<string | null>
  pickFiles?: () => Promise<string[]>
  openSessionWindow?: (id: string) => Promise<boolean>
  openFolder?: (path: string, target: 'finder' | 'vscode') => Promise<boolean>
  pathForFile?: (file: File) => string
}
const desktop = (): DesktopBridge | undefined => (window as unknown as { vesperDesktop?: DesktopBridge }).vesperDesktop
export const isDesktop = (): boolean => typeof desktop()?.pickFolder === 'function'

export async function openSessionWindow(id: string): Promise<void> {
  try {
    if (!await desktop()?.openSessionWindow?.(id)) toast('Could not open a new window.', 'error')
  } catch (e) { toast(errorText(e), 'error') }
}

export async function openFolderIn(path: string, target: 'finder' | 'vscode'): Promise<void> {
  try {
    if (!await desktop()?.openFolder?.(path, target)) toast(`Could not open this folder in ${target === 'vscode' ? 'VS Code' : 'the file manager'}.`, 'error')
  } catch (e) { toast(errorText(e), 'error') }
}

const IMAGE_MIME = /^image\/(png|jpeg|gif|webp)$/
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const baseName = (p: string): string => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p

function addAttachment(a: Omit<Attachment, 'key'>, target = draftKey(state)): void {
  target = draftAliases.get(target) ?? target
  set(s => {
    const attachments = s.attachments[target] ?? []
    if (attachments.some(x => (a.path && x.path === a.path))) return {}
    if (attachments.length >= 20) { toast('At most 20 attachments per message.', 'error'); return {} }
    return { attachments: { ...s.attachments, [target]: [...attachments, { ...a, key: uid() }] } }
  })
}
export const removeAttachment = (key: string): void => set(s => ({
  attachments: { ...s.attachments, [draftKey(s)]: currentAttachments(s).filter(a => a.key !== key) },
}))

export function attachPath(path: string, kind: Attachment['kind'] = 'file', target = draftKey(state)): void {
  addAttachment({ name: baseName(path), kind, path }, target)
}

/** Native file picker (several files at once). */
export async function pickAttachments(): Promise<void> {
  const target = draftKey(state)
  const paths = await desktop()?.pickFiles?.()
  for (const p of paths ?? []) attachPath(p, /\.(png|jpe?g|gif|webp)$/i.test(p) ? 'image' : 'file', target)
}

/** Native folder picker; the folder goes with the message as a listing the agent can explore. */
export async function pickAttachFolder(): Promise<void> {
  const target = draftKey(state)
  const path = await desktop()?.pickFolder?.()
  if (path) attachPath(path, 'folder', target)
}

const readAsDataUrl = (file: Blob): Promise<string> => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(String(r.result))
  r.onerror = () => reject(r.error)
  r.readAsDataURL(file)
})

/** Files dropped or pasted into the window. On the desktop they go by path; pasted images by content. */
export async function attachFiles(files: File[]): Promise<void> {
  const target = draftKey(state)
  for (const file of files) {
    const path = (() => { try { return desktop()?.pathForFile?.(file) || '' } catch { return '' } })()
    if (path) {
      attachPath(path, IMAGE_MIME.test(file.type) ? 'image' : file.type === '' && !file.size ? 'folder' : 'file', target)
      continue
    }
    if (IMAGE_MIME.test(file.type)) {
      if (file.size > MAX_IMAGE_BYTES) { toast(`${file.name || 'Image'}: images must be under 5 MB.`, 'error'); continue }
      const url = await readAsDataUrl(file)
      const name = file.name && file.name !== 'image.png' ? file.name : `pasted-${new Date().toTimeString().slice(0, 8).replace(/:/g, '')}.png`
      addAttachment({ name, kind: 'image', preview: url, image: { name, mime: file.type, data: url.slice(url.indexOf(',') + 1) } }, target)
      continue
    }
    toast(`${file.name}: drop files in the desktop app, or use @path in the message.`, 'error')
  }
}

// ---------------- plan, undo, context ----------------
let beforePlan: AgentMode = 'ask'

/** Changes what the agent may do alone. Remembers the mode to return to after plan mode. */
export async function setAgentMode(mode: AgentMode): Promise<boolean> {
  const now = state.server?.settings.agent_mode
  if (mode === 'plan' && now && now !== 'plan') beforePlan = now
  return saveSettings({ agent_mode: mode })
}
export const togglePlan = (): Promise<boolean> =>
  setAgentMode(state.server?.settings.agent_mode === 'plan' ? beforePlan : 'plan')

/** The user read the plan and said yes: leave plan mode and let the agent do it. */
export async function runPlan(): Promise<void> {
  if (currentBusy(state)) return
  const chatId = state.currentId
  if (await setAgentMode(beforePlan)) {
    if (state.currentId !== chatId) { toast('Return to the plan to run it.', 'error'); return }
    await send('The plan is approved. Carry it out now.')
  }
}

/** Puts back the files the agent's latest edits changed in the open chat. */
export async function undoChanges(): Promise<void> {
  const chat = currentChat(state)
  if (!chat || state.streamingIds.includes(chat.id)) return
  try {
    const { paths, note } = await api.undo(chat.id, chat.sessionId)
    if (!paths.length) { toast('Nothing to undo in this chat.'); return }
    if (note) window.dispatchEvent(new Event('vesper:git-changed'))
    const changed = (m: Msg): boolean => m.tools.some(t => (t.name === 'edit_file' || t.name === 'write_file') && t.ok)
    const target = [...chat.messages].reverse().find(m => m.role === 'bot' && !m.undone && changed(m))
    if (target) patchMessage(chat.id, target.id, () => ({ undone: true }))
    toast(`${paths.length} file${paths.length === 1 ? '' : 's'} put back: ${paths.map(folderName).slice(0, 4).join(', ')}${paths.length > 4 ? ', …' : ''}${note ? ` ${note}` : ''}`)
  } catch (e) {
    toast(errorText(e), 'error')
  }
}

/**
 * Goes back to before one of the user's own messages: it and everything after it are forgotten.
 * `files` also puts back files the agent changed since then. With `edit`, the new text is sent at
 * once; otherwise the old text returns to the message box to change and send.
 */
export async function rewindTo(chatId: string, msgId: string, opts: { files: boolean; edit?: string }): Promise<boolean> {
  const chat = state.chats.find(c => c.id === chatId)
  if (!chat) return false
  if (state.streamingIds.includes(chatId)) { toast('Stop the answer first.', 'error'); return false }
  if (opts.edit !== undefined && !readyToSend()) return false // keep the conversation if the new text cannot go
  const at = chat.messages.findIndex(m => m.id === msgId)
  const msg = chat.messages[at]
  if (at < 0 || !msg || msg.role !== 'user') return false
  const fromEnd = chat.messages.slice(at + 1).filter(m => m.role === 'user').length
  let result: { text: string; restored: string[]; files_known: boolean }
  try {
    result = await api.rewindChat(chatId, chat.sessionId, fromEnd, msg.text, opts.files, msg.turnId)
  } catch (e) {
    toast(errorText(e), 'error')
    return false
  }
  // Going back before the first message leaves an empty saved conversation, which the sidebar
  // does not list; unlink it so the chat (and the text put back) stays open. The engine keeps the
  // same session for this chat, so the next message saves to it again.
  const emptied = at === 0
  set(st => ({ chats: st.chats.map(c => (c.id === chatId
    ? { ...c, messages: c.messages.slice(0, at), updated: Date.now(), ...(emptied ? { sessionId: undefined, syncedAt: undefined } : {}) }
    : c)) }))
  const n = result.restored.length
  if (n) toast(`${n} file${n === 1 ? '' : 's'} put back: ${result.restored.map(folderName).slice(0, 4).join(', ')}${n > 4 ? ', …' : ''}`)
  else if (opts.files && !result.files_known) toast('Only the conversation went back: this message is older than file tracking.')
  else if (opts.files) toast('Went back. No files had changed since that message.')
  if (state.currentId !== chatId) selectChat(chatId)
  if (opts.edit !== undefined) await send(opts.edit)
  else setDraft(result.text)
  return true
}

/** Replaces the older turns of the open chat with a summary, to free context. */
export async function compactChat(): Promise<void> {
  const chat = currentChat(state)
  if (!chat || state.streamingIds.includes(chat.id)) return
  toast('Summarizing the conversation…')
  try {
    const r = await api.compact(chat.id, chat.sessionId)
    if (r.context_report) set(s => ({ chats: s.chats.map(c => c.id === chat.id ? { ...c, contextReport: r.context_report } : c) }))
    toast(r.compacted ? `Older turns are now a summary (about ${r.before} → ${r.context} tokens).` : 'Nothing to summarize yet.')
  } catch (e) {
    toast(errorText(e), 'error')
  }
}

export const COMMANDS: { name: string; hint: string }[] = [
  { name: '/new', hint: 'start a new conversation' },
  { name: '/plan', hint: 'plan mode on or off' },
  { name: '/undo', hint: 'put back the files of the last change' },
  { name: '/rewind', hint: 'go back before your last message to edit it (or use ↶ on any message)' },
  { name: '/compact', hint: 'summarize older turns to free context' },
  { name: '/transcript', hint: 'show thinking and output in full' },
  { name: '/rename', hint: 'rename this conversation: /rename New title' },
  { name: '/delete', hint: 'delete this conversation' },
  { name: '/model', hint: 'pick a model' },
  { name: '/permissions', hint: 'what runs without asking' },
  { name: '/check', hint: 'tests or lint after edits: /check npm test, /check off' },
]

/** `/check`: show, set or (with "off") remove the check command of the working folder. */
/** /git auto on|off, a separate copy (worktree) for this chat, and merging or discarding it. */
export async function gitAction(action: GitAction, name?: string): Promise<boolean> {
  let chat = currentChat(state)
  if (!chat) { // the start screen: make the chat now, so the copy belongs to it
    const created: Chat = { id: uid(), title: 'New chat', updated: Date.now(), messages: [],
      cwd: state.server?.settings.workspace || state.server?.home || '' }
    draftAliases.set(draftKey(state), created.id)
    set(st => ({ chats: [created, ...st.chats], currentId: created.id }))
    chat = created
  }
  if (state.streamingIds.includes(chat.id)) { toast('Wait for this chat to finish, or stop it first.'); return false }
  try {
    const r = await api.gitAction(chat.id, chat.sessionId, action, name)
    if (r.cwd) { const cwd = r.cwd; set(st => ({ chats: st.chats.map(c => c.id === chat.id ? { ...c, cwd } : c) })) }
    window.dispatchEvent(new Event('vesper:git-changed'))
    toast([r.message, ...(r.notes ?? [])].join(' '))
    return true
  } catch (e) { toast(errorText(e), 'error'); return false }
}

export async function setCheckCommand(arg: string): Promise<void> {
  const folder = currentChat(state)?.cwd || state.server?.settings.workspace || state.server?.home || ''
  try {
    const off = /^(off|none|clear)$/i.test(arg.trim())
    const res = arg.trim() ? await api.setCheck(folder, off ? '' : arg.trim()) : await api.check(folder)
    window.dispatchEvent(new Event('vesper:check-changed'))
    toast(res.command ? `After edits, Vesper runs ${res.command} and fixes what fails.`
      : `No check command for this folder.${res.suggestion ? ` Try /check ${res.suggestion}` : ' Set one with /check <command>.'}`)
  } catch (e) { toast(errorText(e), 'error') }
}

/** Slash commands typed in the composer. Returns true when the text was one. */
export function runCommand(text: string): boolean {
  const [word = '', ...rest] = text.trim().split(/\s+/)
  const chat = currentChat(state)
  switch (word.toLowerCase()) {
    case '/undo': void undoChanges(); return true
    case '/rewind': {
      const last = chat ? [...chat.messages].reverse().find(m => m.role === 'user') : undefined
      if (!chat || !last) toast('Nothing to go back to yet.')
      else void rewindTo(chat.id, last.id, { files: false })
      return true
    }
    case '/compact': void compactChat(); return true
    case '/plan': void togglePlan(); return true
    case '/new': case '/clear': newChat(); return true
    case '/transcript': case '/verbose': toggleTranscript(); return true
    case '/permissions': case '/think': openSettings('agent'); return true
    case '/model': case '/key': openSettings('models'); return true
    case '/rename':
      if (chat && rest.length) void renameSession(chat.sessionId ?? chat.id, rest.join(' '))
      else toast('Use /rename New title in an open conversation.', 'error')
      return true
    case '/delete':
      if (chat) void deleteSession(chat.sessionId ?? chat.id)
      return true
    case '/check': void setCheckCommand(rest.join(' ')); return true
    default: return false
  }
}

// ---------------- approvals ----------------
/** Answers an approval card. The agent is paused until this arrives. */
export async function respond(chatId: string, msgId: string, approvalId: string, answer: ApprovalAnswer): Promise<void> {
  const setApproval = (patch: Partial<NonNullable<ToolStep['approval']>>): void =>
    patchMessage(chatId, msgId, m => ({
      tools: m.tools.map(t => (t.approval?.id === approvalId ? { ...t, approval: { ...t.approval, ...patch } } : t)),
    }))
  setApproval({ sending: true })
  try {
    await api.approve(approvalId, answer)
    setApproval({ answer, sending: false })
  } catch (e) {
    setApproval({ answer: 'no', sending: false })
    toast(errorText(e), 'error')
  }
}

// ---------------- the working folder ----------------
export const canPickFolder = isDesktop

/** Native folder dialog in the desktop app; the Agent settings tab everywhere else. */
export async function pickFolder(): Promise<void> {
  const bridge = desktop()
  if (!bridge?.pickFolder) {
    openSettings('agent')
    return
  }
  const path = await bridge.pickFolder()
  if (path) await openFolder(path)
}

export async function openFolder(path: string): Promise<boolean> {
  const ok = await saveSettings({ workspace: path })
  if (ok) {
    toast(`Working in ${folderName(path)}`)
    if (state.currentId && currentChat(state)?.messages.length) newChat()
  }
  return ok
}

export const folderName = (path: string): string => path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path
