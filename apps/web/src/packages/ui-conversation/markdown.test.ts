// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { renderMarkdown } from './markdown'

describe('renderMarkdown', () => {
  it('highlights a fenced code block and gives it a header with the language and Copy', () => {
    const html = renderMarkdown('```python\ndef add(a, b):\n    return a + b\n```')
    expect(html).toContain('data-code="1"')
    expect(html).toContain('<span>python</span>')
    expect(html).toContain('data-copy="1"')
    expect(html).toMatch(/<span class="hljs-keyword">def<\/span>/)
  })

  it('keeps only highlight classes and still strips scripts and handlers', () => {
    const html = renderMarkdown('<span class="evil">x</span><img src=x onerror=alert(1)><script>alert(1)</script>\n\n```js\nlet a = "<b>"\n```')
    expect(html).not.toContain('class="evil"')
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('<script')
    expect(html).toContain('&lt;b&gt;')
  })

  it('leaves unknown short text plain', () => {
    expect(renderMarkdown('```\nhello there\n```')).toContain('<span>text</span>')
  })
})
