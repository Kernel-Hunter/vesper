"""Session export: a saved conversation as a Markdown or a self-contained HTML file.

Both show the user's messages, Vesper's answers, and each step it took (folded, with its
output cut to a few thousand characters). Reasoning is left out unless asked for. The HTML
file has no scripts and no outside resources; model text goes through markdown-it with raw
HTML turned off, so nothing in a conversation can run when the file is opened.

Used by `/export` in the terminal, `vesper sessions export`, and the desktop app's
`GET /api/sessions/{id}/export`.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import re
from pathlib import Path

MAX_STEP_OUTPUT = 3000   # characters of a step's output kept in the file
FORMATS = ("md", "html")


def _when(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _cut(text: str) -> str:
    text = str(text or "").rstrip()
    return text if len(text) <= MAX_STEP_OUTPUT else text[:MAX_STEP_OUTPUT] + "\n... (cut)"


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks in `text`."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _title(row: dict) -> str:
    return (row.get("title") or "").strip() or "Vesper conversation"


def _meta(row: dict) -> list[str]:
    parts = [_when(row.get("created_at"))]
    if row.get("model"):
        parts.append(str(row["model"]))
    if row.get("cwd"):
        parts.append(str(row["cwd"]))
    return [p for p in parts if p]


def _turns(messages) -> list[dict]:
    from vesper.coder import transcript

    return transcript(messages)


def file_name(row: dict, fmt: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", _title(row).lower()).strip("-")[:50] or "conversation"
    return f"vesper-{slug}-{str(row.get('id', ''))[:8]}.{fmt}"


# ------------------------------------------------------------------ Markdown
def to_markdown(row: dict, messages, reasoning: bool = False) -> str:
    from vesper import ui

    out = [f"# {_title(row)}", "", "_" + " · ".join(["Vesper", *_meta(row)]) + "_", ""]
    for turn in _turns(messages):
        if turn["role"] == "user":
            out += ["## You", "", turn["text"].strip(), ""]
            continue
        out += ["## Vesper", ""]
        for part in turn["parts"]:
            if part["type"] == "text":
                out += [part["text"].strip(), ""]
            elif part["type"] == "reasoning":
                if reasoning:
                    out += ["<details><summary>Thinking</summary>", "", part["text"].strip(), "", "</details>", ""]
            else:
                title = ui.step_title(part["name"], part.get("args") or {})
                mark = " (failed)" if part.get("ok") is False else ""
                body = _cut(part.get("output") or "")
                out += [f"<details><summary>{_html.escape(title)}{mark}</summary>", ""]
                if body:
                    fence = _fence(body)
                    out += [fence, body, fence, ""]
                out += ["</details>", ""]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#fbfaf7;--fg:#22211e;--muted:#77736a;--line:#e6e2d8;--band:#f1eee6;--code:#f4f2ec;--accent:#5b5bd6;--bad:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--fg:#e9e7e2;--muted:#9a968d;--line:#2c2c30;--band:#222226;--code:#1f1f23;--accent:#9d9dff;--bad:#ff7b6b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:48px 20px 80px}
header{margin-bottom:36px;padding-bottom:18px;border-bottom:1px solid var(--line)}
h1{font-size:26px;line-height:1.25;margin:0 0 6px;letter-spacing:-.01em}
.meta{color:var(--muted);font-size:13px;word-break:break-all}
.user{background:var(--band);border-radius:12px;padding:12px 16px;margin:28px 0 18px;white-space:pre-wrap}
.who{display:block;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.answer p{margin:.6em 0}
.answer a{color:var(--accent)}
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow-x:auto;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
code{font:0.9em ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);padding:.1em .3em;border-radius:4px}
pre code{background:none;padding:0}
details{margin:6px 0;border-left:2px solid var(--line);padding-left:12px}
summary{cursor:pointer;color:var(--muted);font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
details.failed summary{color:var(--bad)}
details.thinking summary{font-family:inherit;font-style:italic}
table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:4px 8px}
blockquote{margin:0;padding-left:12px;border-left:3px solid var(--line);color:var(--muted)}
footer{margin-top:48px;color:var(--muted);font-size:12px}
"""


def _md():
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")


def to_html(row: dict, messages, reasoning: bool = False) -> str:
    from vesper import ui

    md = _md()
    esc = _html.escape
    body = [f"<header><h1>{esc(_title(row))}</h1><div class=\"meta\">{esc(' · '.join(_meta(row)))}</div></header>"]
    for turn in _turns(messages):
        if turn["role"] == "user":
            body.append(f'<section class="user"><span class="who">You</span>{esc(turn["text"].strip())}</section>')
            continue
        parts = []
        for part in turn["parts"]:
            if part["type"] == "text":
                parts.append(md.render(part["text"]))
            elif part["type"] == "reasoning":
                if reasoning:
                    parts.append(f'<details class="thinking"><summary>Thinking</summary>{md.render(part["text"])}</details>')
            else:
                title = ui.step_title(part["name"], part.get("args") or {})
                failed = part.get("ok") is False
                out = _cut(part.get("output") or "")
                cls = ' class="failed"' if failed else ""
                parts.append(f"<details{cls}><summary>{esc(title)}{' (failed)' if failed else ''}</summary>"
                             + (f"<pre>{esc(out)}</pre>" if out else "") + "</details>")
        body.append('<section class="answer">' + "".join(parts) + "</section>")
    body.append("<footer>Exported from Vesper</footer>")
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            f"<title>{esc(_title(row))}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(body)}</main></body></html>\n")


def render(row: dict, messages, fmt: str, reasoning: bool = False) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    return to_html(row, messages, reasoning) if fmt == "html" else to_markdown(row, messages, reasoning)


def write(row: dict, messages, fmt: str, target: Path | None = None, reasoning: bool = False) -> Path:
    """Write the export. `target` may be a folder (the file is named for the conversation) or a file."""
    text = render(row, messages, fmt, reasoning)
    target = Path(target).expanduser() if target else Path.cwd()
    path = target / file_name(row, fmt) if target.is_dir() else target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
