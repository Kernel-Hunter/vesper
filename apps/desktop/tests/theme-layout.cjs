// Verify the actual Chromium theme tokens without touching the user's profile.
const { app, BrowserWindow } = require('electron')
const { readFileSync, mkdtempSync, rmSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const assert = require('node:assert/strict')

const home = mkdtempSync(join(tmpdir(), 'vesper-theme-test-'))
app.setPath('userData', home)
app.commandLine.appendSwitch('disable-gpu')
app.whenReady().then(async () => {
  if (process.platform === 'darwin') app.dock.hide()
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true } })
  let failed = false
  try {
    await win.loadURL('data:text/html,<html><body></body></html>')
    for (const file of ['ui-theme/tokens.css', 'ui-primitives/primitives.css', 'ui-layout/layout.css', 'ui-conversation/conversation.css', 'ui-input/input.css', 'ui-settings/settings.css', 'ui-theme/designs.css']) {
      await win.webContents.insertCSS(readFileSync(join(__dirname, '../../web/src/packages', file), 'utf8'))
    }
    await win.webContents.executeJavaScript(`document.documentElement.className = 'is-desktop is-mac'; document.body.innerHTML =
      '<div class="app"><aside class="sidebar"><div class="chat-row active"><button class="chat-title">Conversation</button></div></aside><main class="main"><header class="topbar"><span class="top-title">Project</span></header><div class="thread"><div class="empty"><h1>What are we building?</h1></div><div class="msg user"><div class="user-col"><div class="bubble">A message</div></div></div></div><div class="composer-wrap"><form class="composer"><textarea>Draft</textarea><button class="send">Send</button></form></div></main></div>'`)
    const themes = ['dark', 'light', 'midnight', 'ocean', 'forest', 'nebula', 'ember', 'rose', 'sand', 'lavender', 'sky', 'workbench', 'synthwave', 'pixel', 'typewriter']
    const rows = await win.webContents.executeJavaScript(`(${JSON.stringify(themes)}).map(theme => {
      document.documentElement.dataset.theme = theme;
      const c = getComputedStyle(document.documentElement);
      const v = name => c.getPropertyValue(name).trim();
      return { theme, bg: v('--bg'), side: v('--bg-side'), layer: v('--layer-2'), text: v('--text'),
        secondary: v('--text-2'), muted: v('--text-3'), accent: v('--accent'), scheme: c.colorScheme };
    })`)
    function luminance(hex) {
      assert.match(hex, /^#[a-f\d]{6}$/i)
      const values = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
      return values.map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
        .reduce((n, v, i) => n + v * [0.2126, 0.7152, 0.0722][i], 0)
    }
    function contrast(a, b) { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05) }
    const failures = []
    for (const row of rows) {
      for (const surface of [row.bg, row.side, row.layer]) {
        for (const [name, color] of [['text', row.text], ['secondary', row.secondary], ['accent', row.accent]]) {
          const ratio = contrast(color, surface)
          if (ratio < 4.5) failures.push(`${row.theme}: ${name} on ${surface} (${ratio.toFixed(2)})`)
        }
      }
      if (contrast(row.muted, row.side) < 3) failures.push(`${row.theme}: muted text on sidebar`)
      if (row.scheme !== (['light', 'sand', 'lavender', 'sky', 'workbench', 'typewriter'].includes(row.theme) ? 'light' : 'dark')) failures.push(`${row.theme}: wrong color scheme`)
    }
    assert.deepEqual(failures, [], failures.join('\n'))
    assert.equal(new Set(rows.map(r => r.bg)).size, rows.length, 'each palette should have a distinct background')
    const designed = themes.filter(t => !['dark', 'light'].includes(t))
    const identities = await win.webContents.executeJavaScript(`(${JSON.stringify(designed)}).map(theme => {
      document.documentElement.dataset.theme = theme;
      const style = selector => getComputedStyle(document.querySelector(selector));
      return { theme, background: style('.main').backgroundImage, sidebar: style('.sidebar').backgroundImage,
        chrome: style('.topbar').backgroundImage, shadow: style('.composer').boxShadow,
        radius: style('.composer').borderRadius, bubble: style('.bubble').borderRadius,
        title: style('.empty h1').fontFamily, font: style('.composer textarea').fontFamily };
    })`)
    for (const design of identities) {
      assert.notEqual(design.background, 'none', `${design.theme}: no designed background`)
      assert.notEqual(design.sidebar, 'none', `${design.theme}: no sidebar material on macOS`)
      assert.notEqual(design.shadow, 'none', `${design.theme}: no composer treatment`)
    }
    assert.equal(new Set(identities.map(d => d.background)).size, designed.length, 'each theme must have its own background design')
    const identity = name => identities.find(d => d.theme === name)
    assert.match(identity('workbench').chrome, /gradient/, 'Workbench needs its classic title bar')
    assert.equal(identity('pixel').radius, '0px', 'Pixel Arcade needs square controls')
    assert.match(identity('typewriter').font, /Courier/i, 'Typewriter needs typewriter typography')
    assert.notEqual(identity('ocean').bubble, identity('midnight').bubble, 'themes must differ beyond color')
    const phosphors = await win.webContents.executeJavaScript(`['green', 'amber', 'cyan', 'white', 'red', 'violet', 'pink', 'blue'].map(color => {
      document.documentElement.dataset.theme = 'terminal'; document.documentElement.dataset.phosphor = color;
      const c = getComputedStyle(document.documentElement);
      return { color, value: c.getPropertyValue('--phos').trim(), bg: c.getPropertyValue('--bg').trim() };
    })`)
    for (const p of phosphors) assert.ok(contrast(p.value, p.bg) >= 4.5, `${p.color}: phosphor contrast`)
    assert.equal(new Set(phosphors.map(p => p.value)).size, phosphors.length, 'each phosphor should be distinct')
    console.log(`Theme tokens: ${rows.length} palettes and ${phosphors.length} phosphors passed contrast checks`)
  } catch (error) {
    console.error(error)
    failed = true
  } finally { win.destroy(); app.exit(failed ? 1 : 0) }
}).catch(error => { console.error(error); app.exit(1) })
app.on('quit', () => { try { rmSync(home, { recursive: true, force: true }) } catch { /* OS may still hold files */ } })
