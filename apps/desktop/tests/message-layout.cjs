// Real Chromium layout regression for sent messages, without an API or the user's home.
const { app, BrowserWindow } = require('electron')
const { readFileSync, mkdtempSync, rmSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const assert = require('node:assert/strict')

const home = mkdtempSync(join(tmpdir(), 'vesper-layout-test-'))
app.setPath('userData', home)
app.commandLine.appendSwitch('disable-gpu')
app.whenReady().then(async () => {
  if (process.platform === 'darwin') app.dock.hide()
  const win = new BrowserWindow({ show: false, width: 900, height: 600, webPreferences: { sandbox: true } })
  let failed = false
  try {
    await win.loadURL('data:text/html,<html><body></body></html>')
    const css = ['ui-theme/tokens.css', 'ui-conversation/conversation.css', 'ui-theme/designs.css']
      .map(file => readFileSync(join(__dirname, '../../web/src/packages', file), 'utf8')).join('\n')
    await win.webContents.insertCSS(`* { box-sizing: border-box } body { margin: 0; font: 15px var(--font) } .col { width: 780px } ${css}`)
    const result = await win.webContents.executeJavaScript(`(() => {
      document.body.innerHTML = '<div class="col"><div class="msg user"><div class="user-col"><div class="bubble"></div></div></div></div>';
      const bubble = document.querySelector('.bubble');
      function measure(text) {
        bubble.textContent = text;
        const node = bubble.firstChild;
        const rects = [...text].map((_, i) => { const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1); const b = r.getBoundingClientRect(); return { x: b.x, y: b.y, right: b.right }; });
        const b = bubble.getBoundingClientRect(), parent = document.querySelector('.user-col').getBoundingClientRect();
        return { rects, left: b.left, right: b.right, parentLeft: parent.left, parentRight: parent.right };
      }
      return ['dark', 'light', 'midnight', 'ocean', 'forest', 'nebula', 'ember', 'rose', 'sand', 'lavender', 'sky', 'terminal', 'workbench', 'synthwave', 'pixel', 'typewriter'].flatMap(theme => {
        document.documentElement.dataset.theme = theme;
        return [240, 780].map(width => {
          document.querySelector('.col').style.width = width + 'px';
          return { theme, width, short: measure('test'), long: measure('a'.repeat(300)) };
        });
      });
    })()`)
    for (const row of result) {
      const label = `${row.theme} at ${row.width}px`
      assert.equal(new Set(row.short.rects.map(r => r.y)).size, 1, `${label}: short message must remain on one line`)
      assert.ok(row.short.rects[3].x > row.short.rects[0].x, `${label}: characters must advance horizontally`)
      assert.ok(new Set(row.long.rects.map(r => r.y)).size > 1, `${label}: long unbroken words must wrap`)
      assert.ok(row.long.left >= row.long.parentLeft - 1 && row.long.right <= row.long.parentRight + 1, `${label}: bubble must fit its column`)
    }
    console.log(`Message layout: short and long text passed in ${result.length} theme/width combinations`)
  } catch (error) {
    console.error(error)
    failed = true
  } finally {
    win.destroy()
    app.exit(failed ? 1 : 0)
  }
}).catch(error => { console.error(error); app.exit(1) })
app.on('quit', () => { try { rmSync(home, { recursive: true, force: true }) } catch { /* OS may still hold files */ } })
