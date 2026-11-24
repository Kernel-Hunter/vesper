"""Use Vesper from Telegram: `vesper telegram`.

The bot runs on your computer and talks to Telegram's Bot API with long polling (standard
library only, no web server, nothing to expose). Every message you send becomes a turn of a
normal Vesper conversation, saved in the same database as the terminal and the desktop app.
You see what Vesper does as it does it: its words stream into a message, each step (file
read, search, edit, command) appears in an activity message with its result, edits show
their diff, and anything that needs a yes arrives with Yes / Always / No buttons.

Only Telegram accounts you paired can use the bot: the first time, `vesper telegram` shows
a six-digit code, and you send `/start CODE` to your bot. The token lives in
`~/.vesper/.env` as TELEGRAM_BOT_TOKEN; paired accounts and the folder are in `config.json`
under `telegram`.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
API = "https://api.telegram.org/bot{token}/{method}"
CHUNK = 3500            # characters of Markdown per message
MAX_HTML = 4000         # Telegram's limit is 4096 characters after entities are parsed; keep a margin
EDIT_EVERY_S = 1.5      # Telegram allows about one edit per second per chat
APPROVAL_TIMEOUT_S = 30 * 60
POLL_S = 30
MAX_WRONG_CODES = 10
ICONS = {
    "read_file": "📖", "list_files": "📁", "glob": "🔎", "grep": "🔎", "change_directory": "📂",
    "write_file": "📝", "edit_file": "✏️", "run_command": "▶️", "run_check": "🧪", "web_fetch": "🌐",
    "web_search": "🌐", "update_tasks": "📋",
}
HELP = """<b>Vesper on Telegram</b>
Send any message and Vesper works on it, in <code>{folder}</code>. You see every step as it happens.

/new start a new conversation
/resume go back to an earlier one
/stop stop what Vesper is doing
/mode how much Vesper asks before acting
/cd <i>folder</i> work in another folder
/undo put back the files of the last change
/status model, folder and conversation
/help this message

While Vesper works, a new message is added to what it is doing."""


class TelegramError(Exception):
    pass


# ---------------------------------------------------------------- the Bot API
def _tls():
    from vesper.webtools import _tls as tls

    return tls()


class Api:
    """The few Bot API methods Vesper uses, over HTTPS with JSON."""

    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, **params):
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        for attempt in range(4):
            req = urllib.request.Request(API.format(token=self.token, method=method), data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=int(params.get("timeout") or 0) + 20, context=_tls()) as r:
                    reply = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    reply = json.loads(e.read())
                except ValueError:
                    raise TelegramError(f"Telegram answered HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 3:
                    raise TelegramError(f"Cannot reach Telegram ({e})") from e
                time.sleep(2 * (attempt + 1))
                continue
            if reply.get("ok"):
                return reply["result"]
            wait = (reply.get("parameters") or {}).get("retry_after")
            if wait and attempt < 3:  # too many requests: Telegram says how long to wait
                time.sleep(min(float(wait), 10))
                continue
            raise TelegramError(reply.get("description") or "Telegram refused the request")
        raise TelegramError("Telegram kept refusing the request")


# ---------------------------------------------------------------- Markdown to Telegram HTML
def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def to_html(md: str) -> str:
    """The Markdown models write, as the HTML subset Telegram shows."""
    out = []
    parts = re.split(r"```([\w+#.-]*)[^\n]*\n(.*?)(?:```|$)", md, flags=re.S)
    for i in range(0, len(parts), 3):
        out.append(_inline(parts[i]))
        if i + 2 < len(parts):
            lang = f' class="language-{parts[i + 1]}"' if parts[i + 1] else ""
            out.append(f"<pre><code{lang}>{html.escape(parts[i + 2].rstrip(), quote=False)}</code></pre>")
    return "".join(out).strip()


def split_md(md: str, size: int = CHUNK) -> list[str]:
    """Markdown cut into pieces that fit one message, closing and reopening code fences."""
    pieces, cur, fence = [], "", ""
    for line in md.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur.strip():
            pieces.append(cur + ("```\n" if fence else ""))
            cur = fence
        while len(line) > size:  # one enormous line
            pieces.append(line[:size])
            line = line[size:]
        cur += line
        if line.lstrip().startswith("```"):
            fence = "" if fence else line.lstrip()
    if cur.strip():
        pieces.append(cur)
    return pieces or [""]


def fit_md(md: str, size: int = CHUNK) -> list[str]:
    """`split_md`, then halve any piece whose HTML would still be too long for one message
    (escaping `<` and `&` and adding tags makes HTML longer than its Markdown)."""
    out = []
    for piece in split_md(md, size):
        if len(to_html(piece)) + 2 > MAX_HTML and size > 200:
            out += fit_md(piece, max(200, len(piece) // 2))
        else:
            out.append(piece)
    return out


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------- one answer, drawn live
def tool_line(name: str, args: dict) -> str:
    from vesper.ui import TOOL_LABELS, summarize_args

    what = summarize_args(name, args)
    return f"{ICONS.get(name, '⚙️')} <b>{esc(TOOL_LABELS.get(name, name))}</b>" + (f" <code>{esc(what)}</code>" if what else "")


def result_text(name: str, output: str, ok: bool) -> str:
    from vesper.ui import summarize_output

    first = summarize_output(name, output, ok, limit=0).plain.splitlines()
    return ("" if ok else "❌ ") + esc(first[0] if first else "done")


class Turn:
    """What one answer looks like in the chat: Vesper's words stream into a message, and the
    steps in between go into an activity message that grows as they happen."""

    def __init__(self, bot: Bot, chat: Chat) -> None:
        self.bot, self.chat = bot, chat
        self.lock = threading.RLock()
        self.text = ""
        self.text_msg: int | None = None
        self.text_sent = ""
        self.steps: list[list[str]] = []   # [line, result, extra html]
        self.log_msg: int | None = None
        self.log_sent = ""
        self.status = "Thinking"
        self.tail: list[str] = []
        self.started = time.monotonic()
        self.last_edit = 0.0
        self.waiting = False
        self.running = True
        self.alive = True       # the ticker's flag; `running` is lowered briefly while drawing
        self.usage: dict | None = None
        self.ticker = threading.Thread(target=self._tick, daemon=True, name="vesper-telegram-tick")
        self.ticker.start()

    # events from Session.ask, the workspace and the approvals
    def event(self, kind: str, data: dict) -> None:
        with self.lock:
            if kind == "token":
                if self.steps or self.log_msg is not None:
                    self._close_log()
                self.text += data["text"]
            elif kind == "reasoning":
                self.status = "Thinking"
            elif kind == "tool_draft":
                self.status = "Preparing a step"
            elif kind == "tool_start" and data["name"] != "update_tasks":
                self._close_text()
                self.steps.append([tool_line(data["name"], data.get("args", {})), "", ""])
                self.status, self.tail = "Working", []
            elif kind == "tool_end" and data["name"] != "update_tasks" and self.steps:
                self.steps[-1][1] = result_text(data["name"], data.get("output", ""), data.get("ok", True))
                if data["name"] in ("run_command", "run_check"):
                    body = "\n".join(data.get("output", "").splitlines()[1:])[-1500:].strip()
                    if body:
                        self.steps[-1][2] = f"<blockquote expandable><pre>{esc(body)}</pre></blockquote>"
                self.status, self.tail = "Thinking", []
            elif kind == "auto" and data.get("kind") == "edit" and self.steps:
                diff = (data.get("diff") or "")[-2500:]
                if diff.strip():
                    self.steps[-1][2] = ('<blockquote expandable><pre><code class="language-diff">'
                                         f"{esc(diff)}</code></pre></blockquote>")
            elif kind == "output":
                self.tail = [*self.tail, data["line"][:200]][-4:]
            elif kind == "tasks":
                marks = {"done": "✅", "active": "▶️"}
                rows = [f"{marks.get(i.get('status'), '▫️')} {esc(i.get('text', ''))}" for i in data["items"]]
                self._send("📋 <b>Plan</b>\n" + "\n".join(rows))
            elif kind == "notice":
                self._send("ℹ️ " + esc(data.get("text", "")))
            elif kind == "cwd":
                self._send(f"📂 Now in <code>{esc(data.get('path', ''))}</code>")
            elif kind == "usage":
                self.usage = data
            self._flush()

    def wait_for_user(self, waiting: bool) -> None:
        with self.lock:
            self.waiting = waiting
            self._flush(force=True)

    def finish(self, error: str | None = None, stopped: bool = False) -> None:
        with self.lock:
            self.running = self.alive = False
            self._close_log()
            self._close_text()
            end = []
            if error:
                end.append("⚠️ " + esc(error))
            if stopped:
                end.append("⏹ Stopped.")
            if self.usage and not error:
                from vesper.ui import fmt_cost, fmt_duration

                bits = [fmt_duration(self.usage["seconds"])]
                if self.usage.get("tools"):
                    bits.append(f"{self.usage['tools']} step{'s' if self.usage['tools'] != 1 else ''}")
                if self.usage.get("cost") is not None:
                    bits.append(fmt_cost(self.usage["cost"]))
                end.append("<i>◦ " + " · ".join(bits) + "</i>")
            if end:
                self._send("\n".join(end))

    # drawing: a message Telegram refuses or a dropped connection must never stop the agent
    def _send(self, body: str) -> int | None:
        return self._draw(None, body)

    def _draw(self, message_id: int | None, body: str) -> int | None:
        try:
            return self.bot.send_or_edit(self.chat.id, message_id, body)
        except TelegramError as e:
            self.bot.say(f"Telegram refused a message: {e}")
            return message_id

    def _log_html(self) -> str:
        rows = []
        for line, result, extra in self.steps:
            rows.append(line + (f"\n   ↳ {result}" if result else ""))
            if extra:
                rows.append(extra)
        if self.running:
            if self.waiting:
                rows.append("<i>🔐 Waiting for your answer…</i>")
            else:
                rows.append(f"<i>⏳ {esc(self.status)}… {int(time.monotonic() - self.started)}s</i>")
            if self.tail:
                rows.append("<pre>" + esc("\n".join(self.tail)) + "</pre>")
        return "\n".join(rows)[-3900:]

    def _flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_edit < EDIT_EVERY_S:
            return
        self.last_edit = time.monotonic()
        if self.text.strip():
            pieces = fit_md(self.text)
            while len(pieces) > 1:  # the answer outgrew one message: finish it, go on in a new one
                self._put_text(to_html(pieces.pop(0)))
                self.text_msg, self.text_sent = None, ""
                self.text = "".join(pieces)
            self._put_text(to_html(self.text) + (" ▍" if self.running else ""))
        elif self.running and (self.steps or self.log_msg is not None
                               or time.monotonic() - self.started > 2):
            body = self._log_html()
            if body != self.log_sent:
                self.log_msg = self._draw(self.log_msg, body)
                self.log_sent = body

    def _put_text(self, body: str) -> None:
        if body and body != self.text_sent:
            self.text_msg = self._draw(self.text_msg, body)
            self.text_sent = body

    def _close_text(self) -> None:
        if self.text.strip():
            running, self.running = self.running, False
            self._flush(force=True)
            self.running = running
        self.text, self.text_msg, self.text_sent = "", None, ""

    def _close_log(self) -> None:
        if self.steps:
            running, self.running = self.running, False
            body = self._log_html()
            if body != self.log_sent:
                self.log_msg = self._draw(self.log_msg, body)
            self.running = running
        elif self.log_msg is not None:  # only the "Thinking…" placeholder: remove it
            self.bot.delete(self.chat.id, self.log_msg)
        self.steps, self.log_msg, self.log_sent, self.tail = [], None, "", []

    def _tick(self) -> None:
        while self.alive:
            time.sleep(EDIT_EVERY_S)
            with self.lock:
                if self.alive and self.running:
                    self._flush(force=True)


# ---------------------------------------------------------------- chats and the bot
class Chat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.session = None
        self.workspace = None
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        self.turn: Turn | None = None

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()


class Bot:
    def __init__(self, api: Api, store, cfg: dict, folder: Path, pairing: bool = False, say=print) -> None:
        self.api, self.store, self.cfg, self.say = api, store, cfg, say
        self.folder = folder
        self.chats: dict[int, Chat] = {}
        self.approvals: dict[str, dict] = {}
        self.code = f"{secrets.randbelow(10**6):06d}" if pairing or not self.allowed_users() else None
        self.offset = 0
        self.username = ""
        self.wrong_codes = 0

    # settings
    def settings(self) -> dict:
        from vesper.onboarding import load_config

        raw = load_config().get("telegram")
        return raw if isinstance(raw, dict) else {}

    def allowed_users(self) -> list[int]:
        return [int(u) for u in self.settings().get("allowed", []) if str(u).lstrip("-").isdigit()]

    def save(self, **values) -> None:
        from vesper.onboarding import save_config

        save_config({"telegram": {**self.settings(), **values}})

    # sending
    def send(self, chat_id: int, body: str, buttons: list | None = None) -> int | None:
        markup = {"inline_keyboard": buttons} if buttons else None
        try:
            return self.api.call("sendMessage", chat_id=chat_id, text=body or "…", parse_mode="HTML",
                                 link_preview_options={"is_disabled": True}, reply_markup=markup)["message_id"]
        except TelegramError as e:
            if "parse" not in str(e).lower() and "tag" not in str(e).lower():
                raise
            plain = html.unescape(re.sub(r"<[^>]+>", "", body))[:4096] or "…"  # HTML it refused: send it plain
            return self.api.call("sendMessage", chat_id=chat_id, text=plain, reply_markup=markup)["message_id"]

    def send_or_edit(self, chat_id: int, message_id: int | None, body: str, buttons: list | None = None) -> int | None:
        if message_id is None:
            return self.send(chat_id, body, buttons)
        try:
            self.api.call("editMessageText", chat_id=chat_id, message_id=message_id, text=body or "…",
                          parse_mode="HTML", link_preview_options={"is_disabled": True},
                          reply_markup={"inline_keyboard": buttons} if buttons else None)
        except TelegramError as e:
            if "not modified" in str(e):
                return message_id
            if "parse" not in str(e).lower() and "tag" not in str(e).lower():
                raise
            plain = html.unescape(re.sub(r"<[^>]+>", "", body))[:4096] or "…"
            try:
                self.api.call("editMessageText", chat_id=chat_id, message_id=message_id, text=plain)
            except TelegramError:
                pass
        return message_id

    def delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.api.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

    # the loop
    def start(self) -> None:
        self.username = self.api.call("getMe").get("username", "")
        self.api.call("setMyCommands", commands=[
            {"command": c, "description": d} for c, d in (
                ("new", "Start a new conversation"), ("resume", "Go back to an earlier conversation"),
                ("stop", "Stop what Vesper is doing"), ("mode", "How much Vesper asks first"),
                ("cd", "Work in another folder"), ("undo", "Put back the last change"),
                ("status", "Model, folder and conversation"), ("help", "What you can do"))])
        if self.code:
            self.say(f"Pair your Telegram account: open https://t.me/{self.username} and send   /start {self.code}")
        else:
            self.say(f"Vesper is on Telegram as @{self.username}, working in {self.folder}. Stop with Ctrl+C.")

    def run(self, stop: threading.Event | None = None) -> None:
        self.start()
        while stop is None or not stop.is_set():
            try:
                updates = self.api.call("getUpdates", offset=self.offset, timeout=POLL_S,
                                        allowed_updates=["message", "callback_query"])
            except TelegramError as e:
                if "conflict" in str(e).lower():
                    raise TelegramError("Another Vesper is already connected to this bot. Stop it first "
                                        "(vesper telegram --stop stops the one in the background).") from e
                self.say(f"Telegram: {e}. Trying again.")
                time.sleep(5)
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                    self.say(f"Could not handle a Telegram update: {e}")

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            return self.on_button(update["callback_query"])
        msg = update.get("message") or {}
        user = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if user is None or chat_id is None:
            return
        if user not in self.allowed_users():
            return self.pair(user, chat_id, msg, text)
        if not text:
            self.send(chat_id, "Vesper reads text messages. Photos, voice notes and files are not supported yet.")
            return
        chat = self.chat(chat_id)
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            return self.command(chat, cmd.split("@")[0].lower(), arg.strip())
        if chat.busy:
            if chat.session.steer(text):
                self.send(chat_id, "➕ Added to what Vesper is doing.")
            else:
                self.send(chat_id, "Vesper is still working. Send /stop to stop it first.")
            return
        self.run_turn(chat, text)

    def pair(self, user: int, chat_id: int, msg: dict, text: str) -> None:
        if self.code and text.replace("/start", "").strip() == self.code:
            self.save(allowed=[*self.allowed_users(), user])
            self.code = None  # one pairing per code: nobody else can join with it
            name = (msg.get("from") or {}).get("first_name") or "there"
            self.say(f"Paired with {name} on Telegram.")
            self.send(chat_id, f"✨ Paired. Hi {esc(name)}!\n\n" + HELP.format(folder=esc(str(self.folder))))
            return
        if self.code and text.startswith("/start "):
            self.wrong_codes += 1
            if self.wrong_codes >= MAX_WRONG_CODES:  # someone is guessing: the code stops working
                self.code = None
                self.say("Too many wrong pairing codes. Run `vesper telegram --pair` for a new one.")
        self.send(chat_id, "🔒 This Vesper bot is private. Its owner pairs a Telegram account from their "
                           "computer with <code>vesper telegram --pair</code>.")

    # conversations
    def chat(self, chat_id: int) -> Chat:
        chat = self.chats.get(chat_id)
        if chat is None:
            chat = self.chats[chat_id] = Chat(chat_id)
            self.open(chat)
        return chat

    def open(self, chat: Chat, row: dict | None = None) -> None:
        from vesper import permissions
        from vesper.coder import Session
        from vesper.tui import context_tokens, make_llm, make_small_llm, make_switcher
        from vesper.workspace import Workspace

        if chat.workspace is None:
            folder = Path(self.settings().get("folder") or self.folder)
            chat.workspace = Workspace(
                folder if folder.is_dir() else self.folder, approve=lambda action: self.approve(chat, action),
                mode=self.settings().get("mode") or "ask",
                on_auto=lambda action: chat.turn and chat.turn.event("auto", action),
                on_output=lambda line: chat.turn and chat.turn.event("output", {"line": line}),
                rules=permissions.load_rules("allow_commands"), hosts=permissions.load_rules("allow_hosts"),
                on_rule=lambda key, rule: permissions.save_rule(rule, key),
            )
        if row is not None and Path(row["cwd"]).is_dir():
            chat.workspace.change_directory(row["cwd"])
        chat.session = Session(
            make_llm(self.cfg), chat.workspace, self.store, session_id=row["id"] if row else None,
            source="telegram", provider=self.cfg.get("provider", ""), model=self.cfg.get("model", ""),
            context_tokens=context_tokens(self.cfg),
        )
        chat.session.small_llm = make_small_llm(self.cfg)
        chat.session.switcher = make_switcher(self.cfg)

    def run_turn(self, chat: Chat, text: str) -> None:
        chat.stop.clear()
        turn = chat.turn = Turn(self, chat)

        def work() -> None:
            from vesper.tui import friendly

            gen = chat.session.ask(text, shown=text)
            error = None
            try:
                for kind, data in gen:
                    if chat.stop.is_set():
                        break
                    turn.event(kind, data)
            except BaseException as exc:  # noqa: BLE001 - shown in the chat
                error = friendly(exc, self.cfg)
            finally:
                gen.close()
                turn.finish(error, stopped=chat.stop.is_set())
                if chat.turn is turn:
                    chat.turn = None

        chat.worker = threading.Thread(target=work, daemon=True, name="vesper-telegram-turn")
        chat.worker.start()

    # approvals: asked from the agent's thread, answered by a button
    def approve(self, chat: Chat, action: dict) -> str:
        aid = secrets.token_hex(6)
        slot = {"event": threading.Event(), "answer": "no", "chat": chat.id}
        self.approvals[aid] = slot
        kind, strict, rule = action["kind"], action.get("strict"), action.get("rule")
        if kind == "edit":
            head = ("📝 <b>Create " if action.get("new") else "✏️ <b>Edit ") + f"{esc(action.get('path', ''))}?</b>"
            diff = (action.get("diff") or "")[:3000]
            body = head + (f'\n<pre><code class="language-diff">{esc(diff)}</code></pre>' if diff.strip() else "")
            always = "Always for edits"
        elif kind == "web":
            body = f"🌐 <b>Open this page?</b>\n{esc(action.get('url', ''))}"
            always = f"Always for {rule}" if rule else "Always"
        elif kind == "mcp":
            body = (f"🧩 <b>Use {esc(action.get('mcp_tool', ''))}</b> from {esc(action.get('server', ''))}?\n"
                    f"<pre>{esc(action.get('args', ''))[:2000]}</pre>")
            always = "Always for this tool"
        else:
            body = (f"▶️ <b>Run this command?</b>\n<pre>$ {esc(action.get('command', ''))}</pre>\n"
                    f"in <code>{esc(action.get('cwd', ''))}</code>")
            always = f"Always for {rule}" if rule else "Always for this one"
        if strict:
            body += "\n\n⚠️ This can do serious damage or touches credentials, so it needs a yes every time."
        row = [{"text": "✅ Yes", "callback_data": f"ap:{aid}:yes"}]
        if not strict:
            row.append({"text": f"♾ {always}"[:40], "callback_data": f"ap:{aid}:always"})
        buttons = [row, [{"text": "✖ No", "callback_data": f"ap:{aid}:no"}]]
        if chat.turn:
            chat.turn.wait_for_user(True)
        slot["message"] = self.send(chat.id, body, buttons)
        answered = slot["event"].wait(APPROVAL_TIMEOUT_S)
        self.approvals.pop(aid, None)
        if chat.turn:
            chat.turn.wait_for_user(False)
        answer = slot["answer"] if answered and not chat.stop.is_set() else "no"
        label = {"yes": "✅ Allowed", "always": "♾ Allowed from now on", "no": "✖ Declined"}[answer]
        try:
            self.send_or_edit(chat.id, slot["message"], body + f"\n\n<b>{label}</b>")
        except TelegramError:
            pass
        return answer

    def on_button(self, query: dict) -> None:
        user = (query.get("from") or {}).get("id")
        data = query.get("data") or ""
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        if user not in self.allowed_users() or chat_id is None:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text="This bot is private.")
            return
        self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        kind, _, rest = data.partition(":")
        if kind == "ap":
            aid, _, answer = rest.partition(":")
            slot = self.approvals.get(aid)
            if slot is not None and slot["chat"] == chat_id and answer in ("yes", "always", "no"):
                slot["answer"] = answer
                slot["event"].set()
        elif kind == "rs":
            self.resume(self.chat(chat_id), rest)
        elif kind == "md":
            self.set_mode(self.chat(chat_id), rest)

    # commands
    def command(self, chat: Chat, cmd: str, arg: str) -> None:
        if cmd in ("/start", "/help"):
            self.send(chat.id, HELP.format(folder=esc(str(chat.workspace.root))))
        elif cmd == "/new":
            self.stop(chat, wait=True)
            self.open(chat)
            self.send(chat.id, "🆕 New conversation. What should Vesper do?")
        elif cmd in ("/resume", "/history", "/sessions"):
            self.list_sessions(chat)
        elif cmd == "/stop":
            self.send(chat.id, "⏹ Stopping…" if self.stop(chat) else "Vesper is not working on anything.")
        elif cmd == "/mode":
            if arg:
                self.set_mode(chat, arg)
                return
            from vesper.ui import MODES

            buttons = [[{"text": ("● " if m == chat.workspace.mode else "") + m, "callback_data": f"md:{m}"}]
                       for m in MODES]
            rows = "\n".join(f"<b>{esc(m)}</b>: {esc(d)}" for m, d in MODES.items())
            self.send(chat.id, f"How much should Vesper ask before acting?\n\n{rows}", buttons)
        elif cmd == "/cd":
            if not arg:
                self.send(chat.id, f"📂 <code>{esc(str(chat.workspace.root))}</code>\nUse /cd <i>folder</i> to move.")
                return
            out = chat.workspace.change_directory(arg)
            if out.startswith("Error"):
                self.send(chat.id, "⚠️ " + esc(out.removeprefix("Error:").strip()))
            else:
                self.save(folder=str(chat.workspace.root))
                self.send(chat.id, f"📂 Now in <code>{esc(str(chat.workspace.root))}</code>")
        elif cmd == "/undo":
            if chat.busy:
                self.send(chat.id, "Vesper is working. /stop first, then /undo.")
                return
            paths = chat.session.undo()
            self.send(chat.id, ("↩️ Put back:\n" + "\n".join(f"<code>{esc(p)}</code>" for p in paths))
                      if paths else "Nothing to undo.")
        elif cmd == "/status":
            from vesper.ui import model_label

            s = chat.session
            self.send(chat.id, "\n".join([
                f"🤖 <b>Model</b> {esc(model_label(self.cfg))}",
                f"📂 <b>Folder</b> <code>{esc(str(chat.workspace.root))}</code>",
                f"🛡 <b>Mode</b> {esc(chat.workspace.mode)}",
                f"💬 <b>Conversation</b> {esc(s.title or 'new')} · {len(s.history)} messages",
            ]))
        else:
            self.send(chat.id, f"Unknown command {esc(cmd)}. /help lists what you can do.")

    def stop(self, chat: Chat, wait: bool = False) -> bool:
        if not chat.busy:
            return False
        chat.stop.set()
        chat.workspace.cancel()
        for slot in list(self.approvals.values()):
            if slot["chat"] == chat.id:
                slot["event"].set()
        if wait and chat.worker is not None:
            chat.worker.join(10)  # let it end before its session is swapped
        return True

    def set_mode(self, chat: Chat, mode: str) -> None:
        from vesper.ui import MODES

        if mode not in MODES:
            self.send(chat.id, "Modes: " + ", ".join(MODES))
            return
        chat.workspace.mode = mode
        chat.workspace.always.clear()
        self.save(mode=mode)
        self.send(chat.id, f"🛡 Mode is now <b>{esc(mode)}</b>: {esc(MODES[mode])}.")

    def list_sessions(self, chat: Chat) -> None:
        from vesper.ui import fmt_ago

        rows = self.store.list_sessions(9) if self.store is not None else []
        rows = [r for r in rows if r["id"] != chat.session.id][:8]
        if not rows:
            self.send(chat.id, "No earlier conversations yet.")
            return
        buttons = [[{"text": f"{(r['title'] or 'untitled')[:40]} · {fmt_ago(r['updated_at'])}",
                     "callback_data": f"rs:{r['id']}"}] for r in rows]
        self.send(chat.id, "💬 <b>Your conversations</b>\nTap one to continue it.", buttons)

    def resume(self, chat: Chat, session_id: str) -> None:
        row = self.store.get_session(session_id) if self.store is not None else None
        if row is None:
            self.send(chat.id, "That conversation is gone.")
            return
        self.stop(chat, wait=True)
        self.open(chat, row)
        self.send(chat.id, f"↺ <b>Resumed</b> {esc(row['title'] or 'untitled')}")
        for piece in history_messages(chat.session.history):
            self.send(chat.id, piece)
        self.send(chat.id, "— <i>you are here</i> —\nSend a message to continue.")


def history_messages(messages: list, max_turns: int = 10) -> list[str]:
    """A saved conversation as a few Telegram messages: what you asked and what Vesper did."""
    from vesper.coder import transcript

    turns = transcript(messages)
    shown = turns[-max_turns:]
    blocks = [f"<i>⋯ {len(turns) - len(shown)} earlier messages</i>"] if len(turns) > len(shown) else []
    for t in shown:
        if t["role"] == "user":
            blocks.append("👤 <b>You</b>\n" + esc(t["text"][:800]))
            continue
        steps = [p for p in t["parts"] if p["type"] == "tool"]
        words = "\n\n".join(p["text"] for p in t["parts"] if p["type"] == "text").strip()
        body = "✦ <b>Vesper</b>"
        if steps:
            body += f" <i>· {len(steps)} step{'s' if len(steps) != 1 else ''}</i>"
        if words:
            body += "\n" + to_html(words[:1500] + ("…" if len(words) > 1500 else ""))
        blocks.append(body)
    out, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) > 3800 and cur:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- running it in the background (macOS)
LABEL = "dev.vesper.telegram"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_text(program: list[str], log: Path) -> str:
    args = "\n".join(f"    <string>{html.escape(a)}</string>" for a in program)
    path = html.escape(os.environ.get("PATH", "/usr/bin:/bin"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{html.escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{html.escape(str(log))}</string>
</dict>
</plist>
"""


def background(on: bool, folder: Path) -> int:
    import subprocess

    from vesper.onboarding import home
    from vesper.ui import note

    if sys.platform != "darwin":
        note("Running in the background is set up on macOS only. Elsewhere, keep `vesper telegram` running "
             "(for example in tmux or as a systemd service).")
        return 1
    domain = f"gui/{os.getuid()}"
    path = plist_path()
    subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
    if not on:
        path.unlink(missing_ok=True)
        note("The Telegram bot no longer runs in the background.")
        return 0
    exe = shutil.which("vesper") or sys.argv[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist_text([exe, "telegram", "--folder", str(folder)], home() / "telegram.log"))
    done = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if done.returncode != 0:
        note(f"Could not start it in the background: {done.stderr.strip()}")
        return 1
    note(f"The Telegram bot now runs in the background and starts again when you log in. Log: {home() / 'telegram.log'}")
    note("Stop it with: vesper telegram --stop")
    return 0


# ---------------------------------------------------------------- vesper telegram
SETUP = """
  Connect Vesper to Telegram (about one minute):
    1. In Telegram, open  https://t.me/BotFather  and send  /newbot
    2. Pick a name (for example "My Vesper") and a username that ends in "bot"
    3. BotFather replies with a token like 123456789:AAE...  Copy it.
"""


def get_token(interactive: bool) -> str | None:
    from vesper import providers as prov
    from vesper.ui import console, note

    prov.refresh_keys()
    token = os.environ.get(TOKEN_KEY, "").strip()
    if token:
        return token
    if not interactive:
        note(f"No Telegram bot token yet. Run `vesper telegram` once in a terminal to connect it ({TOKEN_KEY}).")
        return None
    console.print(SETUP)
    while True:
        try:
            token = input("  Paste the token here: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print()
            return None
        if not token:
            return None
        try:
            me = Api(token).call("getMe")
        except TelegramError as e:
            note(f"Telegram did not accept that token ({e}). Copy it again from BotFather.")
            continue
        prov.save_user_settings({TOKEN_KEY: token})
        note(f"Saved. Your bot is @{me.get('username')}.")
        return token


def command(argv: list[str]) -> int:
    import argparse

    from vesper.tui import load_config, open_store
    from vesper.ui import apply_theme, err_console, note

    ap = argparse.ArgumentParser(prog="vesper telegram", description="Use Vesper from Telegram.")
    ap.add_argument("--folder", help="the folder Vesper works in (default: the current one)")
    ap.add_argument("--pair", action="store_true", help="show a code to pair another Telegram account")
    ap.add_argument("--background", action="store_true", help="keep the bot running, also after a restart (macOS)")
    ap.add_argument("--stop", action="store_true", help="stop the bot running in the background")
    args = ap.parse_args(argv)
    cfg = load_config()
    apply_theme(cfg.get("theme"))
    folder = Path(args.folder).expanduser().resolve() if args.folder else Path.cwd().resolve()
    if args.stop:
        return background(False, folder)
    token = get_token(sys.stdin.isatty())
    if not token:
        return 1
    if not cfg.get("provider") and not os.environ.get("VESPER_FAKE_LLM"):
        note("Pick a model first: run `vesper` once in a terminal.")
        return 1
    if args.background:
        if not Bot(Api(token), None, cfg, folder).allowed_users():
            note("Pair your Telegram account first: run `vesper telegram` once and send the code to your bot.")
            return 1
        return background(True, folder)
    bot = Bot(Api(token), open_store(), cfg, folder, pairing=args.pair, say=note)
    try:
        bot.run()
    except KeyboardInterrupt:
        note("Telegram bot stopped.")
    except TelegramError as e:
        err_console.print(str(e))
        return 1
    return 0
