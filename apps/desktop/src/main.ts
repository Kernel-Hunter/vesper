// Vesper desktop shell: Electron main process + the Python engine as a
// separate child process + the engine's web UI as the renderer.

import { mkdtempSync, rmSync } from 'node:fs'
import { stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { isAbsolute, join } from 'node:path'
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  nativeTheme,
  session,
  shell,
  type MenuItemConstructorOptions,
} from 'electron'
import { Engine, EngineError, type EngineInfo } from './engine.js'

const SMOKE = process.argv.includes('--smoke')
const SMOKE_TIMEOUT_MS = 90_000
const WINDOWS_TITLEBAR_HEIGHT = 44
const ALLOWED_PERMISSIONS = new Set(['clipboard-sanitized-write'])

let smokeHome: string | undefined
let engineEnv: NodeJS.ProcessEnv = process.env
if (SMOKE) {
  // Throwaway data folders and a fake chat model: the smoke test needs no
  // network, no API key, and leaves nothing behind.
  smokeHome = mkdtempSync(join(tmpdir(), 'vesper-smoke-'))
  // Chromium writes a few files into userData while it shuts down, after any
  // cleanup could run, so it gets one fixed folder that each smoke run resets.
  const smokeUserData = join(tmpdir(), 'vesper-smoke-electron')
  rmSync(smokeUserData, { recursive: true, force: true })
  app.setPath('userData', smokeUserData)
  app.commandLine.appendSwitch('disable-http-cache')
  engineEnv = { ...process.env, VESPER_FAKE_LLM: '1', VESPER_HOME: smokeHome }
}

// Vesper keeps no secrets in Electron (API keys live in the engine's ~/.vesper/.env).
// Without this, Chromium creates a "Vesper Safe Storage" Keychain item, and since each
// unsigned build has a new signature, macOS asks for Keychain access after every update.
if (process.platform === 'darwin') app.commandLine.appendSwitch('use-mock-keychain')

const engine = new Engine(engineEnv)
let mainWindow: BrowserWindow | undefined
let quitting = false

function titleBarSymbolColor(): string {
  return nativeTheme.shouldUseDarkColors ? '#f9fafb' : '#0f1115'
}

function sameOrigin(url: string): boolean {
  try {
    return engine.info !== undefined && new URL(url).origin === engine.info.origin
  } catch {
    return false
  }
}

function openExternally(url: string): void {
  try {
    if (['http:', 'https:'].includes(new URL(url).protocol)) void shell.openExternal(url)
  } catch {
    // not a URL; ignore
  }
}

/** Title bar and background per platform: the page draws its own header on macOS and Windows. */
function platformFrame(): Electron.BrowserWindowConstructorOptions {
  switch (process.platform) {
    case 'darwin':
      // Traffic lights sit inside the page's sidebar, which shows the native
      // translucent sidebar material through a transparent window.
      return {
        titleBarStyle: 'hiddenInset',
        trafficLightPosition: { x: 16, y: 18 },
        backgroundColor: '#00000000',
        vibrancy: 'sidebar',
        visualEffectState: 'active',
      }
    case 'win32':
      return {
        titleBarStyle: 'hidden',
        titleBarOverlay: { color: '#00000000', symbolColor: titleBarSymbolColor(), height: WINDOWS_TITLEBAR_HEIGHT },
      }
    default:
      return {}
  }
}

/** Keeps the window on the engine's page: other links open in the browser, nothing else loads. */
function guardNavigation(contents: Electron.WebContents): void {
  contents.setWindowOpenHandler(({ url }) => {
    openExternally(url)
    return { action: 'deny' }
  })
  contents.on('will-navigate', (event, url) => {
    if (!sameOrigin(url)) {
      event.preventDefault()
      openExternally(url)
    }
  })
  contents.on('will-attach-webview', event => event.preventDefault())
}

/** Right-click menu: editing commands in text fields, Copy on selected text, nothing elsewhere. */
function attachContextMenu(window: BrowserWindow): void {
  window.webContents.on('context-menu', (_event, params) => {
    const flags = params.editFlags
    const template: MenuItemConstructorOptions[] = params.isEditable
      ? [
          { role: 'undo', enabled: flags.canUndo },
          { role: 'redo', enabled: flags.canRedo },
          { type: 'separator' },
          { role: 'cut', enabled: flags.canCut },
          { role: 'copy', enabled: flags.canCopy },
          { role: 'paste', enabled: flags.canPaste },
          { type: 'separator' },
          { role: 'selectAll', enabled: flags.canSelectAll },
        ]
      : params.selectionText ? [{ role: 'copy', enabled: flags.canCopy }] : []
    if (template.length) Menu.buildFromTemplate(template).popup({ window })
  })
}

function createWindow(info: EngineInfo, show = true, sessionId?: string): BrowserWindow {
  const window = new BrowserWindow({
    title: 'Vesper',
    width: 1280,
    height: 820,
    minWidth: 860,
    minHeight: 560,
    show: false,
    ...platformFrame(),
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      webviewTag: false,
      spellcheck: false,
      devTools: !app.isPackaged,
    },
  })
  guardNavigation(window.webContents)
  attachContextMenu(window)
  if (show) window.once('ready-to-show', () => window.show())
  window.on('closed', () => {
    if (mainWindow === window) mainWindow = undefined
  })
  const url = new URL(info.url)
  if (sessionId) url.searchParams.set('session', sessionId)
  void window.loadURL(url.toString())
  return window
}

// Native folder picker for the page (see preload.ts). Only our own page may ask.
ipcMain.handle('vesper:pick-folder', async (event) => {
  if (!sameOrigin(event.senderFrame?.url ?? '')) return null
  const owner = BrowserWindow.fromWebContents(event.sender)
  const options = { title: 'Open a folder', properties: ['openDirectory' as const, 'createDirectory' as const] }
  const result = owner ? await dialog.showOpenDialog(owner, options) : await dialog.showOpenDialog(options)
  return result.canceled ? null : (result.filePaths[0] ?? null)
})

// Native file picker for attachments: any files, several at once (folders too on macOS).
ipcMain.handle('vesper:pick-files', async (event) => {
  if (!sameOrigin(event.senderFrame?.url ?? '')) return []
  const owner = BrowserWindow.fromWebContents(event.sender)
  const properties: ('openFile' | 'openDirectory' | 'multiSelections')[] = ['openFile', 'multiSelections']
  if (process.platform === 'darwin') properties.push('openDirectory')
  const options = { title: 'Attach files', properties }
  const result = owner ? await dialog.showOpenDialog(owner, options) : await dialog.showOpenDialog(options)
  return result.canceled ? [] : result.filePaths.slice(0, 20)
})

// Explicit user actions only. Never expose a general command runner to the page.
ipcMain.handle('vesper:open-session-window', (event, id: unknown) => {
  if (!sameOrigin(event.senderFrame?.url ?? '') || typeof id !== 'string' || !/^[0-9a-f]{4,32}$/.test(id) || !engine.info) return false
  createWindow(engine.info, !SMOKE, id)
  return true
})

ipcMain.handle('vesper:open-folder', async (event, path: unknown, target: unknown) => {
  if (!sameOrigin(event.senderFrame?.url ?? '') || typeof path !== 'string' || path.length > 4096 ||
      path.includes('\0') || !isAbsolute(path) || (target !== 'finder' && target !== 'vscode')) return false
  try {
    if (!(await stat(path)).isDirectory()) return false
    if (target === 'finder') return (await shell.openPath(path)) === ''
    const url = new URL('vscode://file/')
    url.pathname = path
    await shell.openExternal(url.toString())
    return true
  } catch { return false }
})

function lockDownSession(): void {
  const ses = session.defaultSession
  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    const audio = permission === 'media' && sameOrigin(details.requestingUrl) && sameOrigin(wc.getURL()) &&
      'mediaTypes' in details && details.mediaTypes?.length === 1 && details.mediaTypes[0] === 'audio'
    callback(ALLOWED_PERMISSIONS.has(permission) || audio)
  })
  ses.setPermissionCheckHandler((wc, permission, origin, details) =>
    ALLOWED_PERMISSIONS.has(permission) ||
    (permission === 'media' && sameOrigin(origin) && !!wc && sameOrigin(wc.getURL()) && details.mediaType === 'audio'))
}

function installMenu(): void {
  if (process.platform !== 'darwin') {
    // Chromium handles Ctrl+C/V/X/Z/A in text fields by itself on Windows and
    // Linux; the right-click edit menu covers the rest.
    Menu.setApplicationMenu(null)
    return
  }
  const template: MenuItemConstructorOptions[] = [
    { role: 'appMenu' },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        ...(app.isPackaged ? [] : [{ role: 'toggleDevTools' } as MenuItemConstructorOptions]),
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

function showStartupError(err: unknown): void {
  const message = err instanceof Error ? err.message : String(err)
  const log = err instanceof EngineError && err.log !== '' ? `\n\nLast engine output:\n${err.log.split('\n').slice(-12).join('\n')}` : ''
  dialog.showErrorBox('Vesper could not start',
    `${message}\n\nTry opening Vesper again. If this keeps happening, reinstall it or report the problem with the details below.${log}`)
}

function cleanupSmoke(): void {
  if (smokeHome !== undefined) rmSync(smokeHome, { recursive: true, force: true })
}

async function finishSmoke(code: number, detail: string): Promise<void> {
  if (code === 0) console.log('VESPER_SMOKE ok')
  else console.error(`VESPER_SMOKE failed: ${detail}`)
  quitting = true
  await engine.stop()
  mainWindow?.destroy()
  cleanupSmoke()
  app.exit(code)
}

async function runSmoke(): Promise<void> {
  const deadline = Date.now() + SMOKE_TIMEOUT_MS
  const timer = setTimeout(() => void finishSmoke(1, 'timed out after 90 s'), SMOKE_TIMEOUT_MS)
  try {
    const info = await engine.start()
    mainWindow = createWindow(info, false)
    const contents = mainWindow.webContents
    await new Promise<void>((resolveLoad, rejectLoad) => {
      contents.once('did-finish-load', () => resolveLoad())
      contents.once('did-fail-load', (_e, code, description) => rejectLoad(new Error(`page load failed: ${description} (${code})`)))
    })
    // The UI may render a moment after the load event; poll until it shows text.
    const check = "document.querySelector('#root, #app') !== null && document.body.innerText.length > 0"
    while (Date.now() < deadline) {
      if (await contents.executeJavaScript(check) === true) {
        // Exercise the actual appearance controls, including persistence and terminal phosphors.
        const appearance = await contents.executeJavaScript(`(async () => {
          const wait = () => new Promise(r => setTimeout(r, 50));
          const find = async (selector, text) => {
            for (let i = 0; i < 100; i++) {
              const node = [...document.querySelectorAll(selector)].find(n => n.textContent.includes(text));
              if (node) return node;
              await wait();
            }
            throw new Error('Missing appearance control: ' + text);
          };
          (await find('.side-link', 'Customize')).click();
          (await find('.theme-card', 'Nebula')).click();
          await wait();
          const theme = document.documentElement.dataset.theme;
          const before = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
          const details = document.querySelector('.color-advanced');
          const detailsInitiallyClosed = details && !details.open;
          (await find('.palette-card', 'Ion Cloud')).click(); await wait();
          const coordinated = JSON.parse(localStorage.getItem('vesper.appearance') || '{}').palettes?.nebula;
          const presetSelected = (await find('.palette-card', 'Ion Cloud')).getAttribute('aria-pressed') === 'true';
          (await find('.theme-card', 'Pixel Arcade')).click(); await wait();
          (await find('.theme-card', 'Nebula')).click(); await wait();
          const restoredPreset = (await find('.palette-card', 'Ion Cloud')).getAttribute('aria-pressed') === 'true';
          const basePicker = document.querySelector('input[aria-label="Palette color"]');
          if (!basePicker) throw new Error('Missing palette generator');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(basePicker, '#d43a66');
          basePicker.dispatchEvent(new Event('input', { bubbles: true }));
          basePicker.dispatchEvent(new Event('change', { bubbles: true })); await wait();
          const generated = JSON.parse(localStorage.getItem('vesper.appearance') || '{}').palettes?.nebula;
          (await find('.palette-tones button', 'Light')).click(); await wait();
          const lightSaved = JSON.parse(localStorage.getItem('vesper.appearance') || '{}');
          const generatedLight = lightSaved.palettes?.nebula;
          (await find('.palette-tones button', 'Dark')).click(); await wait();
          const generatedAgain = JSON.parse(localStorage.getItem('vesper.appearance') || '{}').palettes?.nebula;
          (await find('.palette-card', 'Original')).click(); await wait();
          const originalSelected = (await find('.palette-card', 'Original')).getAttribute('aria-pressed') === 'true';
          details.open = true;
          (await find('.accent-swatches button', 'Mint')).click();
          await wait();
          const after = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
          const saved = JSON.parse(localStorage.getItem('vesper.appearance') || '{}');
          const picker = document.querySelector('input[aria-label="Custom accent color"]');
          if (!picker) throw new Error('Missing custom color picker');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(picker, '#336699');
          picker.dispatchEvent(new Event('input', { bubbles: true }));
          picker.dispatchEvent(new Event('change', { bubbles: true }));
          await wait();
          const custom = JSON.parse(localStorage.getItem('vesper.appearance') || '{}');
          (await find('.theme-card', 'Terminal')).click();
          await wait();
          const phosInput = document.querySelector('input[aria-label="Phosphor hex color"]');
          if (!phosInput) throw new Error('Missing phosphor editor');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(phosInput, '#99ff00');
          phosInput.dispatchEvent(new Event('input', { bubbles: true })); await wait();
          const customPhosphor = getComputedStyle(document.documentElement).getPropertyValue('--phos').trim();
          (await find('.swatch', 'Amber')).click();
          await wait();
          const terminal = document.documentElement.dataset.theme;
          const phosphor = document.documentElement.dataset.phosphor;
          const presetPhosphor = getComputedStyle(document.documentElement).getPropertyValue('--phos').trim();
          const designs = {};
          for (const [label, id] of [['Workbench', 'workbench'], ['Synthwave', 'synthwave'], ['Pixel Arcade', 'pixel'], ['Typewriter', 'typewriter']]) {
            (await find('.theme-card', label)).click();
            await wait();
            designs[id] = { theme: document.documentElement.dataset.theme, accent: JSON.parse(localStorage.getItem('vesper.appearance') || '{}').accent,
              background: getComputedStyle(document.querySelector('.main')).backgroundImage,
              radius: getComputedStyle(document.querySelector('.composer')).borderRadius,
              font: getComputedStyle(document.querySelector('.composer textarea')).fontFamily };
          }
          const color = async (label, value) => {
            const input = document.querySelector('input[aria-label="' + label + ' hex color"]');
            if (!input) throw new Error('Missing studio color: ' + label);
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            await wait();
          };
          for (const [label, value] of [['Canvas', '#101828'], ['Sidebar', '#182436'], ['Surfaces', '#22334b'], ['Text', '#f4f6ff'], ['Your messages', '#33455c'], ['Accent', '#f0c060']]) await color(label, value);
          await color('Canvas', '#f4f6ff');
          const contrastWarning = !!document.querySelector('.color-warning');
          document.querySelector('button[aria-label="Reset canvas color"]').click(); await wait();
          const resetCanvas = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
          await color('Canvas', '#101828');
          const choose = async (label, value) => {
            const select = [...document.querySelectorAll('.theme-details label')].find(n => n.textContent.includes(label))?.querySelector('select');
            if (!select) throw new Error('Missing theme detail: ' + label);
            select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })); await wait();
          };
          await choose('Type style', 'serif');
          await choose('Corner style', 'rounded');
          document.querySelector('.theme-art-toggle input').click(); await wait();
          const studio = { saved: JSON.parse(localStorage.getItem('vesper.appearance') || '{}'),
            background: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
            composer: getComputedStyle(document.querySelector('.composer')).backgroundColor,
            font: getComputedStyle(document.querySelector('.composer textarea')).fontFamily,
            radius: getComputedStyle(document.querySelector('.composer')).borderRadius,
            art: document.documentElement.dataset.art, artwork: getComputedStyle(document.querySelector('.main')).backgroundImage,
            contrastWarning, resetCanvas };
          (await find('.theme-card', 'Pixel Arcade')).click(); await wait();
          const pixelBackground = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
          (await find('.theme-card', 'Typewriter')).click(); await wait();
          const restored = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
          return { theme, before, after, savedTheme: saved.theme, savedAccent: saved.accent,
            customAccent: custom.accent, customColor: custom.customAccent, terminal, phosphor, customPhosphor, presetPhosphor, designs, studio, pixelBackground, restored,
            detailsInitiallyClosed, coordinated, presetSelected, restoredPreset, generated, generatedLight, generatedAgain, savedSeed: lightSaved.paletteSeeds?.nebula, originalSelected };
        })()`)
        if (appearance.theme !== 'nebula' || appearance.before === appearance.after || appearance.savedTheme !== 'nebula' ||
            appearance.savedAccent !== 'mint' || appearance.customAccent !== 'custom' || appearance.customColor !== '#336699' ||
            !appearance.detailsInitiallyClosed || !appearance.presetSelected || !appearance.restoredPreset || !appearance.originalSelected ||
            appearance.coordinated?.background !== '#191c35' || appearance.coordinated?.accent !== '#c8c2ff' ||
            Object.keys(appearance.generated ?? {}).length !== 6 || appearance.generated.background === appearance.coordinated.background ||
            appearance.generated.sidebar === appearance.coordinated.sidebar || appearance.generated.surface === appearance.coordinated.surface ||
            appearance.generated.bubble === appearance.coordinated.bubble || appearance.generated.accent === appearance.coordinated.accent ||
            appearance.generatedLight?.background === appearance.generated.background || appearance.savedSeed !== '#d43a66' ||
            JSON.stringify(appearance.generatedAgain) !== JSON.stringify(appearance.generated) ||
            appearance.terminal !== 'terminal' || appearance.phosphor !== 'amber' || appearance.customPhosphor !== '#99ff00' || appearance.presetPhosphor !== '#ffb000' ||
            Object.entries(appearance.designs as Record<string, { theme: string; accent: string; background: string }>).some(([id, d]) => d.theme !== id || d.accent !== 'theme' || d.background === 'none') ||
            appearance.designs.pixel.radius !== '0px' || !appearance.designs.typewriter.font.includes('Courier') ||
            appearance.studio.saved.palettes?.typewriter?.background !== '#101828' || appearance.studio.saved.palettes?.typewriter?.surface !== '#22334b' ||
            appearance.studio.saved.palettes?.typewriter?.accent !== '#f0c060' || appearance.studio.saved.styles?.typewriter?.font !== 'serif' ||
            appearance.studio.background !== '#101828' || appearance.studio.composer !== 'rgb(34, 51, 75)' ||
            !appearance.studio.font.includes('Georgia') || appearance.studio.radius !== '32px' || appearance.studio.art !== 'off' || appearance.studio.artwork !== 'none' ||
            !appearance.studio.contrastWarning || appearance.studio.resetCanvas !== '#f5edda' ||
            appearance.pixelBackground === '#101828' || appearance.restored !== '#101828') {
          throw new Error('appearance controls did not apply or persist')
        }
        // Exercise the restricted native bridge and the separate-window URL in a real renderer.
        const bridge = await contents.executeJavaScript(`(async () => ({
          invalidId: await window.vesperDesktop.openSessionWindow('../bad'),
          invalidPath: await window.vesperDesktop.openFolder('relative/path', 'finder'),
          opened: await window.vesperDesktop.openSessionWindow('abcdef123456')
        }))()`)
        if (bridge.invalidId !== false || bridge.invalidPath !== false || bridge.opened !== true) throw new Error('native bridge failed')
        const extra = BrowserWindow.getAllWindows().find(w => w !== mainWindow)
        if (!extra) throw new Error('new conversation window was not created')
        if (extra.webContents.isLoading()) await new Promise<void>((resolveLoad, rejectLoad) => {
          extra.webContents.once('did-finish-load', () => resolveLoad())
          extra.webContents.once('did-fail-load', (_e, code, description) => rejectLoad(new Error(`new window failed: ${description} (${code})`)))
        })
        // The test ID does not exist: its expected 404 toast proves the new renderer tried to open it.
        const until = Math.min(deadline, Date.now() + 10000)
        let triedSession = false
        while (Date.now() < until && !triedSession) {
          triedSession = await extra.webContents.executeJavaScript("document.body.innerText.includes('That conversation is not in the history.')")
          if (!triedSession) await new Promise(r => setTimeout(r, 100))
        }
        if (!triedSession) throw new Error('new window did not load its requested session')
        const initialTheme = await extra.webContents.executeJavaScript("({ theme: document.documentElement.dataset.theme, bg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() })")
        if (initialTheme.theme !== 'typewriter' || initialTheme.bg !== '#101828') throw new Error('new window did not restore theme colors')
        await contents.executeJavaScript(`(() => {
          const input = document.querySelector('input[aria-label="Canvas hex color"]');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '#142538');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })()`)
        let synced = false
        while (Date.now() < until && !synced) {
          synced = await extra.webContents.executeJavaScript("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() === '#142538'")
          if (!synced) await new Promise(r => setTimeout(r, 100))
        }
        if (!synced) throw new Error('theme edits did not sync to the other window')
        const generatedColors = await contents.executeJavaScript(`(() => {
          const input = document.querySelector('input[aria-label="Palette color"]');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '#b46599');
          input.dispatchEvent(new Event('input', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true }));
          const css = getComputedStyle(document.documentElement);
          return ['--bg', '--bg-side', '--layer-1', '--accent'].map(n => css.getPropertyValue(n).trim());
        })()`)
        const paletteDeadline = Math.min(deadline, Date.now() + 5000)
        let paletteSynced = false
        while (Date.now() < paletteDeadline && !paletteSynced) {
          const other = await extra.webContents.executeJavaScript("['--bg', '--bg-side', '--layer-1', '--accent'].map(n => getComputedStyle(document.documentElement).getPropertyValue(n).trim())")
          paletteSynced = JSON.stringify(other) === JSON.stringify(generatedColors)
          if (!paletteSynced) await new Promise(r => setTimeout(r, 100))
        }
        if (!paletteSynced) throw new Error('generated palette did not sync to the other window')
        extra.destroy()
        clearTimeout(timer)
        await finishSmoke(0, '')
        return
      }
      await new Promise(r => setTimeout(r, 500))
    }
  } catch (err) {
    clearTimeout(timer)
    const log = err instanceof EngineError && err.log !== '' ? `\n${err.log}` : ''
    await finishSmoke(1, `${err instanceof Error ? err.message : String(err)}${log}`)
  }
}

async function launch(): Promise<void> {
  let info: EngineInfo
  try {
    info = await engine.start()
  } catch (err) {
    showStartupError(err)
    quitting = true
    app.exit(1)
    return
  }
  mainWindow = createWindow(info)
}

engine.on('restarted', (info: EngineInfo) => {
  for (const window of BrowserWindow.getAllWindows()) void window.loadURL(info.url)
})
engine.on('failed', (err: EngineError) => {
  if (quitting) return
  if (SMOKE) {
    void finishSmoke(1, err.message)
    return
  }
  showStartupError(err)
  quitting = true
  app.exit(1)
})

if (!SMOKE && !app.requestSingleInstanceLock()) {
  app.exit(0)
} else {
  app.on('second-instance', () => {
    const window = mainWindow ?? BrowserWindow.getAllWindows()[0]
    if (!window) return
    if (window.isMinimized()) window.restore()
    window.show()
    window.focus()
  })

  nativeTheme.on('updated', () => {
    if (process.platform !== 'win32') return
    for (const window of BrowserWindow.getAllWindows()) {
      window.setTitleBarOverlay({ color: '#00000000', symbolColor: titleBarSymbolColor(), height: WINDOWS_TITLEBAR_HEIGHT })
    }
  })

  app.on('web-contents-created', (_event, contents) => {
    contents.on('will-attach-webview', event => event.preventDefault())
  })

  // macOS convention: closing the last window keeps the app (and engine)
  // running until the user quits; elsewhere closing the window quits.
  app.on('window-all-closed', () => {
    if (SMOKE) return
    if (process.platform !== 'darwin') app.quit()
  })

  app.on('activate', () => {
    if (SMOKE || quitting) return
    if (BrowserWindow.getAllWindows().length === 0 && engine.info !== undefined) mainWindow = createWindow(engine.info)
  })

  /** How many answers are running; 0 when the engine does not answer quickly. */
  const runningAnswers = async (): Promise<number> => {
    const info = engine.info
    if (!info) return 0
    try {
      const res = await fetch(`${info.origin}/api/busy`, { headers: { 'X-Token': info.token }, signal: AbortSignal.timeout(1500) })
      const data = await res.json() as { working?: unknown }
      return typeof data.working === 'number' ? data.working : 0
    } catch { return 0 }
  }

  let askingToQuit = false
  app.on('before-quit', (event) => {
    if (quitting) return
    event.preventDefault()
    if (askingToQuit) return
    askingToQuit = true
    void (async () => {
      const working = SMOKE ? 0 : await runningAnswers()
      if (working > 0) {
        const owner = BrowserWindow.getFocusedWindow() ?? mainWindow ?? undefined
        const options = {
          type: 'warning' as const, buttons: ['Keep working', 'Quit anyway'], defaultId: 0, cancelId: 0,
          message: working === 1 ? 'Vesper is still working on an answer.' : `Vesper is still working on ${working} answers.`,
          detail: 'If you quit now, everything done so far is saved, and the conversation offers a Continue button when you open Vesper again.',
        }
        const { response } = owner ? await dialog.showMessageBox(owner, options) : await dialog.showMessageBox(options)
        if (response !== 1) { askingToQuit = false; return }
      }
      quitting = true
      void engine.stop().finally(() => app.quit())
    })()
  })

  void app.whenReady().then(() => {
    lockDownSession()
    installMenu()
    if (SMOKE) void runSmoke()
    else void launch()
  })
}
