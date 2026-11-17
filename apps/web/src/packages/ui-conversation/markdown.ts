import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: false })

const escape = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

// Code blocks: highlighted, with a header that names the language and copies the code.
// The header uses data attributes only; DOMPurify keeps those and drops buttons and ids.
marked.use({
  renderer: {
    code({ text, lang }) {
      const name = (lang ?? '').trim().split(/\s+/)[0]?.toLowerCase() ?? ''
      let body = escape(text)
      try {
        if (name && hljs.getLanguage(name)) body = hljs.highlight(text, { language: name, ignoreIllegals: true }).value
        else if (text.length < 20_000) {
          const auto = hljs.highlightAuto(text)
          if (auto.relevance >= 5) body = auto.value
        }
      } catch { /* plain text is fine */ }
      return `<div data-code="1"><div data-code-head="1"><span>${escape(name || 'text')}</span><span data-copy="1" role="button" tabindex="0">Copy</span></div><pre><code>${body}</code></pre></div>`
    },
  },
})

/** Only highlight.js token classes survive sanitizing; every other class is dropped. */
const HLJS_CLASS = /^(hljs-[a-z_-]+|language_|function_|class_)( (hljs-[a-z_-]+|language_|function_|class_))*$/

let hooked = false
function ensureHooks(): void {
  if (hooked) return
  hooked = true
  DOMPurify.addHook('afterSanitizeAttributes', node => {
    if (node.tagName === 'A') {
      const href = node.getAttribute('href') ?? ''
      if (!/^https?:\/\//i.test(href)) {
        node.removeAttribute('href')
      } else {
        node.setAttribute('target', '_blank')
        node.setAttribute('rel', 'noopener noreferrer')
      }
    }
  })
  DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
    if (data.attrName === 'class') data.keepAttr = node.tagName === 'SPAN' && HLJS_CLASS.test(data.attrValue)
  })
}

const cache = new Map<string, string>()

export function renderMarkdown(text: string): string {
  const hit = cache.get(text)
  if (hit !== undefined) return hit
  ensureHooks()
  const raw = marked.parse(text, { async: false }) as string
  const clean = DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'img', 'iframe', 'form', 'input', 'button', 'svg', 'math', 'video', 'audio', 'object', 'embed'],
    FORBID_ATTR: ['style', 'id'],
  })
  if (cache.size > 300) cache.clear()
  cache.set(text, clean)
  return clean
}

/** One handler for every code block's Copy label in a rendered answer (click, Enter or Space). */
export function onCodeCopy(e: { target: EventTarget | null; type: string; key?: string }): void {
  const el = e.target as HTMLElement | null
  if (!el?.closest || (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ')) return
  const label = el.closest('[data-copy]') as HTMLElement | null
  const code = label?.closest('[data-code]')?.querySelector('code')
  if (!label || !code) return
  void navigator.clipboard.writeText(code.textContent ?? '').then(() => {
    label.textContent = 'Copied'
    setTimeout(() => { label.textContent = 'Copy' }, 1400)
  }, () => undefined)
}
