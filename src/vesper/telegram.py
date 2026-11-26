"""Use Vesper from Telegram.

Setting it up is one guided step, `/telegram` inside Vesper (or `vesper telegram` in a
terminal): paste the token BotFather gave you, tap Start in your bot, confirm it is you,
done. From then on the bot runs in the background as a login service.

The bot talks to Telegram's Bot API with long polling (standard library only: no web
server, nothing exposed). Every message becomes a turn of a normal Vesper conversation, saved
in the same database as the terminal and the desktop app. The chat shows what Vesper does as
it does it: its answer streams into a message, each step goes into an activity message with
its result, edits show their diff, commands their output, and anything that needs a yes comes
with Yes / Always / No buttons (a typed "yes" or "no" works too). Photos reach the model as
images; other files are saved and handed to it by path.

Reliability rules, learned from how other agents do this:
- Only one poller per token: it holds an exclusive lock on `~/.vesper/telegram/poller.lock`.
- The update offset is saved before an update is handled, so a crash or restart never runs
  the same task twice; messages that waited while the bot was off are answered with a note
  instead of being run hours late.
- A send that may have reached Telegram is never repeated; edits and reads are retried.
- HTML is checked before it is sent and falls back to plain text, and messages are split by
  Telegram's own length rule (UTF-16 units).
- `/telegram` shows the health of every part: token, pairing, service, lock, heartbeat.

Files: the token is TELEGRAM_BOT_TOKEN in `~/.vesper/.env`; `~/.vesper/telegram/` holds
settings.json (paired accounts, pending pairing codes, folder, mode), state.json (offset,
heartbeat, last error), the lock, an inbox for received files, and the log.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

# Windows locks are mandatory: a locked byte cannot be read by anyone else. Lock one byte
# far past the pid, so another process can still read who holds the lock.
_WIN_LOCK_AT = 1 << 20

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
CHUNK = 3500            # characters of Markdown per message before it is turned into HTML
MAX_UNITS = 4000        # Telegram's limit is 4096 UTF-16 units; keep a margin
EDIT_EVERY_S = 1.2      # Telegram allows about one edit per second per chat
APPROVAL_TIMEOUT_S = 30 * 60
POLL_S = 30
CONFLICT_WAITS = (15, 25, 35, 45, 55)   # another poller has the token: wait, then give up
OFFLINE_AFTER_S = 120   # a message older than this when it arrives was sent while the bot was off
STALE_HEARTBEAT_S = 3 * POLL_S
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CODE_TTL_S = 3600
MAX_PENDING = 3
MAX_FILE_BYTES = 20 * 1024 * 1024       # what the Bot API lets a bot download
SETUP_WAIT_S = 5 * 60
IDEMPOTENT = {"getMe", "getUpdates", "getFile", "editMessageText", "deleteMessage", "setMyCommands",
              "answerCallbackQuery", "setMessageReaction", "sendChatAction", "deleteWebhook"}
ICONS = {
    "read_file": "📖", "list_files": "📁", "glob": "🔎", "grep": "🔎", "change_directory": "📂",
    "write_file": "📝", "edit_file": "✏️", "run_command": "▶️", "run_check": "🧪", "web_fetch": "🌐",
    "web_search": "🌐", "update_tasks": "📋", "command_output": "▶️",
}
COMMANDS = (("new", "Start a new conversation"), ("resume", "Go back to an earlier conversation"),
            ("stop", "Stop what Vesper is doing"), ("mode", "How much Vesper asks first"),
            ("cd", "Work in another folder"), ("undo", "Put back the last change"),
            ("status", "Model, folder and conversation"), ("help", "What you can do"))
HELP = """<b>Vesper on Telegram</b>
Send any message and Vesper works on it, in <code>{folder}</code>. You see every step as it happens. Photos and files work too.

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
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class AlreadyRunning(Exception):
    pass


def home() -> Path:
    from vesper.onboarding import home as vesper_home

    return vesper_home() / "telegram"


def log_path() -> Path:
    return home() / "telegram.log"


def units(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _try_lock(f) -> bool:
    """Take an exclusive lock on an open file without waiting. False when someone holds it."""
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(_WIN_LOCK_AT)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _lock(f) -> None:
    if fcntl is not None:
        fcntl.flock(f, fcntl.LOCK_EX)
        return
    while not _try_lock(f):
        time.sleep(0.05)


def _unlock(f) -> None:
    if fcntl is not None:
        fcntl.flock(f, fcntl.LOCK_UN)
    else:
        f.flush()
        f.seek(_WIN_LOCK_AT)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _file_lock(name: str):
    """An exclusive lock between Vesper processes (the background bot and the terminal both write)."""
    path = home() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        _lock(f)
        try:
            yield
        finally:
            _unlock(f)


# ---------------------------------------------------------------- the live feed
FEED_MAX_BYTES = 2_000_000


def feed_path() -> Path:
    return home() / "live.jsonl"


def _trim(value, limit: int = 2000):
    """Arguments as they go into the feed: long strings (a whole file being written) cut short."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _trim(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v, limit) for v in value[:50]]
    return value


class Feed:
    """What the bot is doing, one JSON line per event, so a terminal can follow along
    (`vesper telegram watch`, and the idle prompt of any open Vesper). Writing never raises:
    a full disk costs the mirror, never the turn."""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def write(self, chat: int, kind: str, **data) -> None:
        try:
            line = json.dumps({"t": time.time(), "chat": chat, "kind": kind, **_trim(data)},
                              ensure_ascii=False, default=str) + "\n"
            with self.lock:
                path = feed_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    if path.stat().st_size > FEED_MAX_BYTES:
                        os.replace(path, path.with_name(path.name + ".1"))
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
        except Exception:  # noqa: BLE001 - the mirror is a nice-to-have
            pass


class FeedReader:
    """Follows the feed from where it is now: only what happens after it starts. Copes with
    half-written lines and with the file being rotated or recreated."""

    def __init__(self) -> None:
        try:
            st = feed_path().stat()
            self.ino, self.offset = st.st_ino, st.st_size
        except OSError:
            self.ino, self.offset = None, 0
        self.rest = b""

    def read(self) -> list[dict]:
        path = feed_path()
        try:
            st = path.stat()
        except OSError:
            return []
        if st.st_ino != self.ino or st.st_size < self.offset:  # a new file: read it from the start
            self.ino, self.offset, self.rest = st.st_ino, 0, b""
        if st.st_size == self.offset:
            return []
        try:
            with open(path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(min(st.st_size - self.offset, 4_000_000))
        except OSError:
            return []
        self.offset += len(chunk)
        lines = (self.rest + chunk).split(b"\n")
        self.rest = lines.pop()
        out = []
        for line in lines:
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if isinstance(event, dict) and "kind" in event:
                    out.append(event)
        return out


# ---------------------------------------------------------------- settings and state
def load_settings() -> dict:
    data = _read_json(home() / "settings.json")
    if not data:  # 0.22.0 kept these in config.json
        from vesper.onboarding import load_config

        old = load_config().get("telegram")
        if isinstance(old, dict):
            data = {k: v for k, v in old.items() if k in ("allowed", "folder", "mode")}
    data.setdefault("allowed", [])
    data.setdefault("names", {})
    data.setdefault("pending", [])
    return data


def update_settings(change) -> dict:
    with _file_lock("settings"):
        data = load_settings()
        change(data)
        _write_json(home() / "settings.json", data)
        return data


def allowed_users() -> list[int]:
    return [int(u) for u in load_settings()["allowed"] if str(u).lstrip("-").isdigit()]


def load_state() -> dict:
    return _read_json(home() / "state.json")


def save_state(**values) -> None:
    with _file_lock("state"):
        _write_json(home() / "state.json", {**load_state(), **values})


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def approve_code(code: str) -> dict | None:
    """Pairs the account that asked with this code. Returns who it was, or None."""
    code = code.strip().upper()
    found: dict = {}

    def change(data: dict) -> None:
        now = time.time()
        for p in data["pending"]:
            if p["code"] == code and p["expires"] > now:
                found.update(p)
        data["pending"] = [p for p in data["pending"] if p["code"] != code and p["expires"] > now]
        if found and found["user"] not in data["allowed"]:
            data["allowed"].append(found["user"])
            data["names"][str(found["user"])] = found.get("name", "")

    update_settings(change)
    return found or None


def unpair(user: int) -> None:
    def change(data: dict) -> None:
        data["allowed"] = [u for u in data["allowed"] if u != user]
        data["names"].pop(str(user), None)

    update_settings(change)


# ---------------------------------------------------------------- one poller per token
class PollerLock:
    """Held for the whole life of a poller. A second one fails at once instead of fighting
    the first for updates (Telegram answers that with 409 Conflict)."""

    def __init__(self) -> None:
        self.file = None

    def acquire(self) -> None:
        path = home() / "poller.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        if not _try_lock(f):
            f.close()
            raise AlreadyRunning(lock_holder())
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self.file = f

    def release(self) -> None:
        if self.file is not None:
            _unlock(self.file)
            self.file.close()
            self.file = None


def lock_holder() -> int | None:
    """The pid of the running poller, or None when nobody polls."""
    path = home() / "poller.lock"
    if not path.exists():
        return None
    with open(path, "a+") as f:
        if not _try_lock(f):
            f.seek(0)
            text = f.read().strip()
            return int(text) if text.isdigit() else -1
        _unlock(f)
        return None


def wait_until_free(seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if lock_holder() is None:
            return True
        time.sleep(0.2)
    return lock_holder() is None


# ---------------------------------------------------------------- the Bot API
def _tls():
    from vesper.webtools import _tls as tls

    return tls()


def _never_sent(e: Exception) -> bool:
    """True when a request certainly did not reach Telegram (so sending it again is safe)."""
    reason = getattr(e, "reason", e)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror)) or "Name or service" in str(reason)


class Api:
    """The Bot API methods Vesper uses, over HTTPS with JSON."""

    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, **params):
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        safe = method in IDEMPOTENT
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
                    raise TelegramError(f"Telegram answered HTTP {e.code}", e.code) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 3 or not (safe or _never_sent(e)):
                    raise TelegramError(f"Cannot reach Telegram ({getattr(e, 'reason', e)})") from e
                time.sleep(1 + attempt)
                continue
            if reply.get("ok"):
                return reply["result"]
            wait = (reply.get("parameters") or {}).get("retry_after")
            if wait and attempt < 3:  # flood control: Telegram says how long to wait
                time.sleep(min(float(wait), 30))
                continue
            raise TelegramError(reply.get("description") or "Telegram refused the request", reply.get("error_code"))
        raise TelegramError("Telegram kept refusing the request")

    def download(self, file_id: str) -> tuple[bytes, str]:
        info = self.call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > MAX_FILE_BYTES or not info.get("file_path"):
            raise TelegramError("The file is larger than the 20 MB a bot can download.")
        url = FILE_API.format(token=self.token, path=info["file_path"])
        with urllib.request.urlopen(url, timeout=120, context=_tls()) as r:
            data = r.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise TelegramError("The file is larger than the 20 MB a bot can download.")
        return data, info["file_path"]


# ---------------------------------------------------------------- Markdown to Telegram HTML
TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote",
        "tg-spoiler", "span"}


def valid_html(text: str) -> bool:
    """Whether Telegram's parser will accept this: known tags, closed in the right order."""
    stack = []
    for m in re.finditer(r"<(/?)([a-z-]+)([^>]*)>", text):
        closing, tag = m.group(1), m.group(2)
        if tag not in TAGS:
            return False
        if closing:
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack and "<" not in re.sub(r"<(/?)([a-z-]+)([^>]*)>", "", text)


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _inline(text: str) -> str:
    codes: list[str] = []

    def keep(m: re.Match) -> str:  # code spans first, so ** inside them is not bold
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    t = re.sub(r"`([^`\n]+)`", keep, text)
    t = html.escape(t, quote=False)
    t = re.sub(r"(?m)^#{1,6}\s+(.+)$", lambda m: "<b>" + m.group(1).replace("**", "") + "</b>", t)
    t = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', t)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], t)


def to_html(md: str) -> str:
    """The Markdown models write, as the HTML subset Telegram shows."""
    out = []
    parts = re.split(r"```([\w+#.-]*)[^\n]*\n(.*?)(?:```|$)", md, flags=re.S)
    for i in range(0, len(parts), 3):
        out.append(_inline(parts[i]))
        if i + 2 < len(parts):
            lang = f' class="language-{parts[i + 1]}"' if parts[i + 1] else ""
            out.append(f"<pre><code{lang}>{html.escape(parts[i + 2].rstrip(), quote=False)}</code></pre>")
    result = "".join(out).strip()
    return result if valid_html(result) else html.escape(md, quote=False)


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
    """`split_md`, then halve any piece whose HTML would still be too long for one message."""
    out = []
    for piece in split_md(md, size):
        if units(to_html(piece)) + 2 > MAX_UNITS and size > 200:
            out += fit_md(piece, max(200, len(piece) // 2))
        else:
            out.append(piece)
    return out


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def clip(body: str, limit: int = MAX_UNITS) -> str:
    """Cut HTML that is too long, closing it safely (the cut part becomes plain text)."""
    if units(body) <= limit:
        return body
    text = strip_html(body)
    while units(text) > limit - 2:
        text = text[: int(len(text) * 0.9)]
    return esc(text) + " …"


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
    steps in between go into an activity message that grows as they happen. Nothing here may
    raise into the agent: a message Telegram refuses is logged and the work goes on."""

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

    def mirror(self, kind: str, **data) -> None:
        self.bot.feed.write(self.chat.id, kind, name=self.chat.name, **data)

    def event(self, kind: str, data: dict) -> None:
        if kind == "tool_start" and data["name"] != "update_tasks":
            self.mirror("tool_start", tool=data["name"], args=data.get("args", {}))
        elif kind == "tool_end" and data["name"] != "update_tasks":
            self.mirror("tool_end", tool=data["name"], output=data.get("output", "")[:6000],
                        ok=data.get("ok", True), ms=data.get("ms"))
        elif kind == "auto" and data.get("kind") == "edit":
            self.mirror("diff", path=data.get("path", ""), diff=(data.get("diff") or "")[-6000:], new=data.get("new"))
        elif kind in ("tasks", "notice", "cwd", "commit"):
            self.mirror(kind, **data)
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
                        self.steps[-1][2] = f"<blockquote expandable>{esc(body)}</blockquote>"
                self.status, self.tail = "Thinking", []
            elif kind == "auto" and data.get("kind") == "edit" and self.steps:
                diff = (data.get("diff") or "")[-2500:]
                if diff.strip():
                    self.steps[-1][2] = f"<blockquote expandable>{esc(diff)}</blockquote>"
            elif kind == "output":
                self.tail = [*self.tail, data["line"][:200]][-4:]
            elif kind == "tasks":
                marks = {"done": "✅", "active": "▶️"}
                rows = [f"{marks.get(i.get('status'), '▫️')} {esc(i.get('text', ''))}" for i in data["items"]]
                self._send("📋 <b>Plan</b>\n" + "\n".join(rows))
            elif kind == "notice":
                self._send("ℹ️ " + esc(data.get("text", "")))
            elif kind == "commit":
                if data.get("sha"):
                    self._send(f"🌿 Committed <code>{esc(data['sha'][:7])}</code> {esc(data.get('message', ''))}")
                else:
                    self._send("🌿 " + esc(data.get("error") or "Not committed."))
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
        usage = self.usage or {}
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
                from vesper.ui import fmt_duration

                bits = [fmt_duration(self.usage["seconds"])]
                if self.usage.get("tools"):
                    bits.append(f"{self.usage['tools']} step{'s' if self.usage['tools'] != 1 else ''}")
                end.append("<i>◦ " + " · ".join(bits) + "</i>")
            self.mirror("done", error=error, stopped=stopped,
                        seconds=usage.get("seconds"), tools=usage.get("tools"))
            # the one message of a turn that notifies: the phone buzzes when the work is done
            self._draw(None, "\n".join(end) or "<i>◦ done</i>", silent=False)

    # drawing
    def _send(self, body: str) -> int | None:
        return self._draw(None, body)

    def _draw(self, message_id: int | None, body: str, silent: bool = True) -> int | None:
        try:
            return self.bot.send_or_edit(self.chat.id, message_id, body, silent=silent)
        except TelegramError as e:
            self.bot.log_error(f"Telegram refused a message: {e}")
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
        while len(rows) > 1 and units("\n".join(rows)) > MAX_UNITS:  # the oldest steps make room
            rows.pop(0)
        return clip("\n".join(rows))

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
            self.mirror("answer", text=self.text)
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
def sender_name(msg: dict) -> str:
    sender = msg.get("from") or {}
    name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name")))) or "someone"
    if sender.get("username"):
        name += f" (@{sender['username']})"
    return name


class Chat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.session = None
        self.workspace = None
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        self.turn: Turn | None = None
        self.name = ""

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()


class Bot:
    def __init__(self, api: Api, store, cfg: dict, folder: Path, say=print) -> None:
        self.api, self.store, self.cfg, self.say = api, store, cfg, say
        self.folder = folder
        self.chats: dict[int, Chat] = {}
        self.approvals: dict[str, dict] = {}
        self.feed = Feed()
        self.offset = 0
        self.username = ""
        self.started_at = time.time()

    def settings(self) -> dict:
        return load_settings()

    def allowed_users(self) -> list[int]:
        return allowed_users()

    def save(self, **values) -> None:
        update_settings(lambda data: data.update(values))

    def log_error(self, text: str) -> None:
        self.say(text)
        with contextlib.suppress(OSError):
            save_state(last_error=text, last_error_at=time.time())

    # sending
    def send(self, chat_id: int, body: str, buttons: list | None = None, silent: bool = True) -> int | None:
        return self.send_or_edit(chat_id, None, body, buttons, silent)

    def send_or_edit(self, chat_id: int, message_id: int | None, body: str, buttons: list | None = None,
                     silent: bool = True) -> int | None:
        body = clip(body or "…")
        markup = {"inline_keyboard": buttons} if buttons else None
        formatted = valid_html(body)
        params = {"chat_id": chat_id, "text": body if formatted else strip_html(body),
                  "parse_mode": "HTML" if formatted else None, "reply_markup": markup,
                  "link_preview_options": {"is_disabled": True}}
        try:
            if message_id is None:
                return self.api.call("sendMessage", disable_notification=silent, **params)["message_id"]
            self.api.call("editMessageText", message_id=message_id, **params)
            return message_id
        except TelegramError as e:
            text = str(e).lower()
            if "not modified" in text:
                return message_id
            if "parse" not in text and "entities" not in text:
                raise
            # Telegram did not accept the HTML (and so did not deliver it): send the words plain
            params.update(text=strip_html(body)[:4000] or "…", parse_mode=None)
            if message_id is None:
                return self.api.call("sendMessage", disable_notification=silent, **params)["message_id"]
            self.api.call("editMessageText", message_id=message_id, **params)
            return message_id

    def delete(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramError):
            self.api.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def acknowledge(self, chat_id: int, message_id: int | None) -> None:
        """👀 on the message and "typing…" at the top: Vesper got it."""
        with contextlib.suppress(TelegramError):
            if message_id:
                self.api.call("setMessageReaction", chat_id=chat_id, message_id=message_id,
                              reaction=[{"type": "emoji", "emoji": "👀"}])
            self.api.call("sendChatAction", chat_id=chat_id, action="typing")

    # the loop
    def start(self) -> None:
        me = self.api.call("getMe")
        self.username = me.get("username", "")
        with contextlib.suppress(TelegramError):
            self.api.call("deleteWebhook")  # a webhook set elsewhere would block getUpdates
        self.api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        self.offset = int(load_state().get("offset") or 0)
        save_state(username=self.username, pid=os.getpid(), started=time.time(), heartbeat=time.time(),
                   last_error=None)
        self.say(f"Vesper is on Telegram as @{self.username}, working in {self.folder}.")

    def run(self, stop: threading.Event | None = None) -> None:
        lock = PollerLock()
        try:
            lock.acquire()
        except AlreadyRunning as e:
            raise TelegramError(f"The Telegram bot is already running (process {e.args[0]}). "
                                "Type /telegram in Vesper to see it.") from None
        try:
            self.start()
            conflicts = 0
            while stop is None or not stop.is_set():
                try:
                    updates = self.api.call("getUpdates", offset=self.offset, timeout=POLL_S,
                                            allowed_updates=["message", "callback_query"])
                    conflicts = 0
                except TelegramError as e:
                    if e.code == 409 or "conflict" in str(e).lower():
                        if conflicts == len(CONFLICT_WAITS):
                            raise TelegramError(
                                "Another program is reading this bot's messages (Telegram says 'Conflict'). "
                                "Only one can. Stop the other one, or make a new token with /revoke in "
                                "@BotFather and set it up again with /telegram setup.") from e
                        self.log_error(f"Telegram: another program uses this bot; trying again in "
                                       f"{CONFLICT_WAITS[conflicts]}s.")
                        time.sleep(CONFLICT_WAITS[conflicts])
                        conflicts += 1
                        continue
                    if e.code == 401:
                        raise TelegramError("Telegram no longer accepts this bot's token. Set it up again "
                                            "with /telegram setup.") from e
                    self.log_error(f"Telegram: {e}. Trying again.")
                    time.sleep(5)
                    continue
                save_state(heartbeat=time.time())
                for update in updates:
                    self.offset = update["update_id"] + 1
                    save_state(offset=self.offset)  # before handling: a crash never runs it twice
                    try:
                        self.handle(update)
                    except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                        self.log_error(f"Could not handle a Telegram update: {e}")
        finally:
            self.shutdown()
            lock.release()

    def shutdown(self, wait_s: float = 5) -> None:
        """Stops every running turn and closes its open questions (as "no"), so no thread is
        left waiting for a button nobody can press any more."""
        for chat in list(self.chats.values()):
            chat.stop.set()
            if chat.workspace is not None:
                with contextlib.suppress(Exception):
                    chat.workspace.cancel()
        for slot in list(self.approvals.values()):
            slot["event"].set()
        end = time.monotonic() + wait_s
        for chat in list(self.chats.values()):
            if chat.worker is not None:
                chat.worker.join(max(0.0, end - time.monotonic()))

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            return self.on_button(update["callback_query"])
        msg = update.get("message") or {}
        user = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        if user is None or chat_id is None:
            return
        if user not in self.allowed_users():
            return self.request_pairing(user, chat_id, msg)
        if msg.get("date") and msg["date"] < self.started_at - OFFLINE_AFTER_S:
            self.send(chat_id, "💤 Vesper was offline when you sent this, so it did not run it. "
                               "Send it again if you still want it.", silent=False)
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        chat = self.chat(chat_id)
        chat.name = self.remember_name(user, msg)
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            return self.command(chat, cmd.split("@")[0].lower(), arg.strip())
        if self.typed_answer(chat, text):
            return
        self.acknowledge(chat_id, msg.get("message_id"))
        try:
            files, images = self.receive_files(msg)
        except TelegramError as e:
            self.send(chat_id, f"⚠️ {esc(str(e))}")
            return
        if not (text or files or images):
            self.send(chat_id, "Vesper reads text, photos and files. Voice notes and stickers are not "
                               "supported yet; type the message instead.")
            return
        if files:
            text = (text or "Look at the attached file.") + "\n\n" + "\n".join(
                f"[The user sent a file over Telegram; it is saved at {p}]" for p in files)
        self.feed.write(chat_id, "user", name=chat.name, text=text or "(an image)",
                        images=len(images), steer=chat.busy)
        if chat.busy:
            if chat.session.steer(text):
                self.send(chat_id, "➕ Added to what Vesper is doing.")
            else:
                self.send(chat_id, "Vesper is still working. Send /stop to stop it first.")
            return
        self.run_turn(chat, text or "Look at the attached image.", images)

    def receive_files(self, msg: dict) -> tuple[list[str], list[dict]]:
        """Photos become images for the model; documents are saved in the inbox."""
        from vesper.attachments import IMAGE_TYPES, MAX_IMAGE_BYTES, image_block

        files, images = [], []
        if msg.get("photo"):
            data, path = self.api.download(msg["photo"][-1]["file_id"])
            if len(data) <= MAX_IMAGE_BYTES:
                images.append(image_block(data, "image/jpeg", Path(path).name))
            else:
                files.append(str(self._save(data, Path(path).name)))
        doc = msg.get("document")
        if doc:
            name = re.sub(r"[^\w.\- ]", "_", doc.get("file_name") or "file")[:120] or "file"
            data, _ = self.api.download(doc["file_id"])
            mime = IMAGE_TYPES.get(Path(name).suffix.lower())
            if mime and len(data) <= MAX_IMAGE_BYTES:
                images.append(image_block(data, mime, name))
            else:
                files.append(str(self._save(data, name)))
        return files, images

    def _save(self, data: bytes, name: str) -> Path:
        inbox = home() / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
        path.write_bytes(data)
        return path

    def remember_name(self, user: int, msg: dict) -> str:
        """The sender's name, saved for `/telegram` the first time a paired user writes
        (pairings made before 0.24 only kept the id)."""
        name = sender_name(msg)
        if name != "someone" and load_settings()["names"].get(str(user)) != name:
            def change(data: dict) -> None:
                data["names"][str(user)] = name
            with contextlib.suppress(OSError):
                update_settings(change)
        return name

    def request_pairing(self, user: int, chat_id: int, msg: dict) -> None:
        name = sender_name(msg)
        code: list[str] = []

        def change(data: dict) -> None:
            now = time.time()
            data["pending"] = [p for p in data["pending"] if p["expires"] > now]
            for p in data["pending"]:
                if p["user"] == user:
                    code.append(p["code"])
                    return
            if len(data["pending"]) < MAX_PENDING:
                code.append(new_code())
                data["pending"].append({"code": code[0], "user": user, "chat": chat_id, "name": name,
                                        "expires": now + CODE_TTL_S})

        update_settings(change)
        if not code:
            self.send(chat_id, "🔒 This Vesper bot is private.")
            return
        self.say(f"{name} asked to use the Telegram bot. To allow it, type in Vesper:  /telegram approve {code[0]}")
        self.send(chat_id, "🔒 This Vesper bot is private.\n\nIf it is yours, type this in Vesper on your "
                           f"computer:\n<code>/telegram approve {code[0]}</code>\n(the code works for one hour)")

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

    def run_turn(self, chat: Chat, text: str, images: list | None = None) -> None:
        chat.stop.clear()
        turn = chat.turn = Turn(self, chat)

        def work() -> None:
            from vesper.tui import friendly

            gen = chat.session.ask(text, shown=text, images=images or None)
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

    # approvals: asked from the agent's thread, answered by a button or a typed yes / no
    def approve(self, chat: Chat, action: dict) -> str:
        aid = secrets.token_hex(6)
        slot = {"event": threading.Event(), "answer": "no", "chat": chat.id}
        self.approvals[aid] = slot
        kind, strict, rule = action["kind"], action.get("strict"), action.get("rule")
        if kind == "edit":
            head = ("📝 <b>Create " if action.get("new") else "✏️ <b>Edit ") + f"{esc(action.get('path', ''))}?</b>"
            diff = (action.get("diff") or "")[:3000]
            body = head + (f"\n<pre>{esc(diff)}</pre>" if diff.strip() else "")
            always = "Always for edits"
        elif kind == "web":
            body = f"🌐 <b>Open this page?</b>\n{esc(action.get('url', ''))}"
            always = f"Always for {rule}" if rule else "Always"
        elif kind == "mcp":
            body = (f"🧩 <b>Use {esc(action.get('mcp_tool', ''))}</b> from {esc(action.get('server', ''))}?\n"
                    f"<pre>{esc(str(action.get('args', ''))[:2000])}</pre>")
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
        self.feed.write(chat.id, "ask", name=chat.name, action={
            k: action.get(k) for k in ("kind", "path", "diff", "new", "command", "cwd", "url", "mcp_tool", "server")
            if action.get(k) is not None})
        try:
            slot["message"] = self.send(chat.id, body, buttons, silent=False)
        except TelegramError as e:
            self.log_error(f"Could not ask for approval: {e}")
            self.approvals.pop(aid, None)
            return "no"
        answered = slot["event"].wait(APPROVAL_TIMEOUT_S)
        self.approvals.pop(aid, None)
        if chat.turn:
            chat.turn.wait_for_user(False)
        answer = slot["answer"] if answered and not chat.stop.is_set() else "no"
        label = {"yes": "✅ Allowed", "always": "♾ Allowed from now on", "no": "✖ Declined"}[answer]
        self.feed.write(chat.id, "answered", name=chat.name, answer=answer)
        with contextlib.suppress(TelegramError):
            self.send_or_edit(chat.id, slot["message"], body + f"\n\n<b>{label}</b>")
        return answer

    def typed_answer(self, chat: Chat, text: str) -> bool:
        """"yes" / "no" / "always" typed while a question is open answers it."""
        word = text.lower().strip(" .!")
        answer = {"yes": "yes", "y": "yes", "ok": "yes", "no": "no", "n": "no", "always": "always"}.get(word)
        slots = [s for s in self.approvals.values() if s["chat"] == chat.id]
        if not answer or not slots:
            return False
        slots[-1]["answer"] = answer
        slots[-1]["event"].set()
        return True

    def on_button(self, query: dict) -> None:
        user = (query.get("from") or {}).get("id")
        data = query.get("data") or ""
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        if user not in self.allowed_users() or chat_id is None:
            with contextlib.suppress(TelegramError):
                self.api.call("answerCallbackQuery", callback_query_id=query["id"], text="This bot is private.")
            return
        with contextlib.suppress(TelegramError):
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        kind, _, rest = data.partition(":")
        if kind == "ap":
            aid, _, answer = rest.partition(":")
            slot = self.approvals.get(aid)
            if slot is not None and slot["chat"] == chat_id and answer in ("yes", "always", "no"):
                slot["answer"] = answer
                slot["event"].set()
            elif slot is None:
                self.send(chat_id, "That question is closed already.")
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
            note = f"\n{esc(chat.session.undo_note)}" if paths and chat.session.undo_note else ""
            self.send(chat.id, ("↩️ Put back:\n" + "\n".join(f"<code>{esc(p)}</code>" for p in paths) + note)
                      if paths else "Nothing to undo.")
        elif cmd == "/status":
            from vesper.ui import model_label

            s = chat.session
            self.send(chat.id, "\n".join([
                f"🤖 <b>Model</b> {esc(model_label(self.cfg))}",
                f"📂 <b>Folder</b> <code>{esc(str(chat.workspace.root))}</code>",
                f"🛡 <b>Mode</b> {esc(chat.workspace.mode)}",
                f"💬 <b>Conversation</b> {esc(s.title or 'new')} · {len(s.history)} messages",
                f"{'⏳ <b>Working</b>' if chat.busy else '💤 <b>Idle</b>'}",
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
        blocks.append(clip(body, 3800))
    out, cur = [], ""
    for b in blocks:
        if units(cur) + units(b) > 3800 and cur:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- the background service
LABEL = "dev.vesper.telegram"
UNIT = "vesper-telegram.service"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def program(folder: Path) -> list[str]:
    exe = shutil.which("vesper") or sys.argv[0]
    return [exe, "telegram", "run", "--folder", str(folder)]


def plist_text(args: list[str], log: Path) -> str:
    items = "\n".join(f"    <string>{html.escape(a)}</string>" for a in args)
    path = html.escape(os.environ.get("PATH", "/usr/bin:/bin"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{html.escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{html.escape(str(log))}</string>
</dict>
</plist>
"""


def unit_text(args: list[str], log: Path) -> str:
    cmd = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"""[Unit]
Description=Vesper Telegram bot
After=network-online.target

[Service]
ExecStart={cmd}
Environment=PATH={os.environ.get("PATH", "/usr/bin:/bin")}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""


def _run(args: list[str]):
    import subprocess

    return subprocess.run(args, capture_output=True, text=True)


def service_installed() -> bool:
    return plist_path().exists() if sys.platform == "darwin" else unit_path().exists()


def service_running() -> bool:
    if sys.platform == "darwin":
        done = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
        return done.returncode == 0 and "state = running" in done.stdout
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return _run(["systemctl", "--user", "is-active", UNIT]).stdout.strip() == "active"
    return False


def stop_service() -> None:
    """Stops and removes the login service, and waits until its poller has let go of the lock."""
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path())])
        plist_path().unlink(missing_ok=True)
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", UNIT])
        unit_path().unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    wait_until_free(15)


def bot_folder(fallback: Path, save: bool = False) -> Path:
    """The folder the bot works in. It is saved once and then kept, so restarting the bot
    from another folder (a terminal, a script) never moves it; /cd in the chat or a new
    setup changes it on purpose. `save` makes `fallback` the saved folder."""
    saved = load_settings().get("folder")
    if saved and Path(saved).is_dir() and not save:
        return Path(saved)
    folder = Path(fallback).expanduser().resolve()
    with contextlib.suppress(OSError):
        update_settings(lambda d: d.update(folder=str(folder)))
    return folder


def start_service(folder: Path) -> str | None:
    """Installs the bot as a login service and starts it. Returns why it failed, or None.
    `folder` is only used when no folder is saved yet."""
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    args = program(bot_folder(folder))
    holder = lock_holder()
    if holder and not service_running():
        return (f"A Vesper Telegram bot is already running in a terminal (process {holder}). "
                "Close it (Ctrl+C in that window) and try again.")
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path())])
        wait_until_free(15)
        plist_path().parent.mkdir(parents=True, exist_ok=True)
        plist_path().write_text(plist_text(args, log))
        done = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())])
        if done.returncode != 0:
            return f"macOS did not start it: {done.stderr.strip() or done.returncode}"
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        unit_path().parent.mkdir(parents=True, exist_ok=True)
        unit_path().write_text(unit_text(args, log))
        _run(["systemctl", "--user", "daemon-reload"])
        done = _run(["systemctl", "--user", "enable", "--now", UNIT])
        if done.returncode != 0:
            return f"systemd did not start it: {done.stderr.strip() or done.returncode}"
    else:
        return ("Running in the background is set up on macOS and Linux. Here, keep  vesper telegram run  "
                "open in a terminal.")
    started = time.time()  # wait for its first heartbeat, so "on" means it really answers
    while time.time() - started < 20:
        if (load_state().get("heartbeat") or 0) >= started and lock_holder():
            return None
        time.sleep(0.5)
    return f"It did not come up within 20 seconds. The log may say why: {log}"


# ---------------------------------------------------------------- health: what /telegram shows
def status(check_token: bool = True) -> dict:
    """Every part of the bot, each with a state: ok, warn or off. `summary` is one line."""
    from vesper import providers as prov

    prov.refresh_keys()
    token = os.environ.get(TOKEN_KEY, "").strip()
    rows: list[tuple[str, str, str]] = []
    out = {"configured": bool(token), "paired": False, "running": False, "rows": rows, "hints": []}
    if not token:
        out["summary"] = "not set up yet"
        out["hints"].append("Type /telegram setup to connect it (about one minute).")
        return out
    username = load_state().get("username") or ""
    if check_token:
        try:
            username = Api(token).call("getMe").get("username", "")
            rows.append(("Bot", f"@{username}", "ok"))
        except TelegramError as e:
            rows.append(("Bot", f"token refused: {e}" if e.code == 401 else f"cannot check: {e}",
                         "warn" if e.code != 401 else "off"))
            if e.code == 401:
                out["hints"].append("Make a new token with /token in @BotFather, then /telegram setup.")
    settings = load_settings()
    names = [settings["names"].get(str(u)) or str(u) for u in settings["allowed"]]
    out["paired"] = bool(names)
    rows.append(("Paired", ", ".join(names) if names else "nobody yet", "ok" if names else "off"))
    pending = [p for p in settings["pending"] if p["expires"] > time.time()]
    for p in pending:
        rows.append(("Waiting", f"{p['name']}: /telegram approve {p['code']}", "warn"))
    holder = lock_holder()
    service = service_running()
    state = load_state()
    beat = time.time() - float(state.get("heartbeat") or 0)
    out["running"] = bool(holder) and beat < STALE_HEARTBEAT_S
    if service and out["running"]:
        rows.append(("Running", "in the background, starts again when you log in", "ok"))
    elif holder and out["running"]:
        rows.append(("Running", f"in a terminal window (process {holder})", "ok"))
    elif holder:
        rows.append(("Running", f"started, but silent for {int(beat)}s: no connection to Telegram?", "warn"))
    else:
        rows.append(("Running", "no", "off"))
    if state.get("last_error") and time.time() - float(state.get("last_error_at") or 0) < 3600:
        rows.append(("Last problem", str(state["last_error"])[:160], "warn"))
    rows.append(("Folder", settings.get("folder") or "the folder where it was started", "ok"))
    rows.append(("Log", str(log_path()), "ok"))
    if not names:
        out["summary"] = "set up, not paired yet"
        out["hints"].append("Type /telegram setup to pair your Telegram account.")
    elif not out["running"]:
        out["summary"] = "set up, not running"
        out["hints"].append("Type /telegram on to start it.")
    else:
        out["summary"] = f"running as @{username}" if username else "running"
        out["hints"].append("It answers while this computer is awake; a sleeping Mac cannot answer.")
        out["hints"].append("/telegram off stops it.")
    return out


# ---------------------------------------------------------------- the guided setup
SETUP_INTRO = """
  Connect Vesper to Telegram. You need a bot of your own, which takes a minute:
    1. In Telegram, open  https://t.me/BotFather  (the real one has a blue check mark)
    2. Send  /newbot , pick a name, then a username ending in "bot"
    3. BotFather answers with a token like  123456789:AAE...  Copy it.
"""


def setup(ask, confirm, say, folder: Path) -> bool:
    """The whole setup, start to finish: token, pairing, background service.
    `ask(prompt, secret)` reads a line (None to give up), `confirm(prompt)` a yes or no."""
    from vesper import providers as prov

    prov.refresh_keys()
    token = os.environ.get(TOKEN_KEY, "").strip()
    me = None
    if token:
        try:
            me = Api(token).call("getMe")
        except TelegramError as e:
            say(f"The saved token does not work ({e}). Paste a new one.")
            token = ""
    if token and me and not confirm(f"Use your bot @{me.get('username')}?"):
        token, me = "", None
    if not token:
        say(SETUP_INTRO)
        while True:
            token = (ask("Paste the token here (Enter to cancel): ", True) or "").strip()
            if not token:
                say("Setup cancelled. Nothing was changed.")
                return False
            if not re.fullmatch(r"\d+:[\w-]{30,}", token):
                say("That does not look like a bot token (it is digits, a colon, then about 35 letters). Try again.")
                continue
            try:
                me = Api(token).call("getMe")
            except TelegramError as e:
                say(f"Telegram did not accept it ({e}). Copy the token from BotFather again.")
                continue
            break
        prov.save_user_settings({TOKEN_KEY: token})
    username = me.get("username", "")
    say(f"✓ Connected to @{username}.")
    stop_service()  # the setup reads the bot's messages itself for a moment
    if lock_holder():
        say("A Vesper Telegram bot is running in a terminal window. Close it (Ctrl+C there) and try again.")
        return False
    if not allowed_users():
        if not pair_owner(Api(token), username, confirm, say):
            return False
    bot_folder(folder, save=True)  # a setup works in the folder it was started from
    if (problem := start_service(folder)) is not None:
        say(problem)
        say("You can still run it in a terminal window:  vesper telegram run")
        return False
    say(f"✓ The bot is running in the background and starts again when you log in. Talk to it at "
        f"https://t.me/{username}")
    say("  It works while this computer is awake. Type /telegram any time to see how it is doing.")
    return True


def pair_owner(api: Api, username: str, confirm, say) -> bool:
    """Waits for the owner's first message to the bot and asks here whether it is really them."""
    lock = PollerLock()
    try:
        lock.acquire()
    except AlreadyRunning:
        say("Another Vesper is reading this bot's messages. Close it and try again.")
        return False
    try:
        with contextlib.suppress(TelegramError):
            api.call("deleteWebhook")
        old = api.call("getUpdates", offset=-1, timeout=0)  # skip whatever was sent before
        offset = old[-1]["update_id"] + 1 if old else 0
        say(f"Now open  https://t.me/{username}  on your phone and tap Start (or send it any message).")
        end = time.monotonic() + SETUP_WAIT_S
        while time.monotonic() < end:
            updates = api.call("getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                sender, chat = msg.get("from") or {}, msg.get("chat") or {}
                if not sender.get("id") or sender.get("is_bot"):
                    continue
                name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name")))) or "someone"
                if sender.get("username"):
                    name += f" (@{sender['username']})"
                if not confirm(f"{name} wrote to the bot. Is that you?"):
                    api.call("sendMessage", chat_id=chat["id"], text="🔒 This Vesper bot is private.")
                    continue
                user = sender["id"]

                def change(data: dict, user=user, name=name) -> None:
                    if user not in data["allowed"]:
                        data["allowed"].append(user)
                    data["names"][str(user)] = name

                update_settings(change)
                api.call("getUpdates", offset=offset, timeout=0)  # mark it read, so the bot does not run it
                save_state(offset=offset)
                api.call("sendMessage", chat_id=chat["id"], parse_mode="HTML",
                         text="✨ <b>Paired.</b> This bot now works only for you.")
                say(f"✓ Paired with {name}.")
                return True
        say("Nobody wrote to the bot within 5 minutes. Run the setup again when you are ready.")
        return False
    finally:
        lock.release()


# ---------------------------------------------------------------- vesper telegram
def command(argv: list[str]) -> int:
    import argparse

    from vesper.tui import load_config, open_store
    from vesper.ui import apply_theme, console, err_console, note

    ap = argparse.ArgumentParser(
        prog="vesper telegram", description="Use Vesper from Telegram.",
        epilog="Inside Vesper, /telegram does the same: /telegram setup, on, off, watch, approve CODE.")
    ap.add_argument("action", nargs="?", default="", choices=["", "setup", "on", "off", "status", "watch", "run", "approve"],
                    help="setup (connect and pair), on / off (background service), status, "
                         "watch (follow what it does), run (in this window)")
    ap.add_argument("code", nargs="?", help="the pairing code, for approve")
    ap.add_argument("--folder", help="the folder Vesper works in (default: the current one)")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)  # 0.22.0 names
    ap.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pair", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    cfg = load_config()
    apply_theme(cfg.get("theme"))
    folder = Path(args.folder).expanduser().resolve() if args.folder else Path.cwd().resolve()
    action = "off" if args.stop else "on" if args.background else "setup" if args.pair else args.action
    tty = sys.stdin.isatty()
    if not action:  # the plain command does what the situation needs
        state = status(check_token=False)
        action = "setup" if tty and not (state["configured"] and state["paired"]) else \
            "status" if tty and state["running"] else "run"
    if action == "off":
        stop_service()
        note("The Telegram bot is off.")
        return 0
    if action == "status":
        show_status(console)
        return 0
    if action == "watch":
        watch(console)
        return 0
    if action == "approve":
        who = approve_code(args.code or "")
        note(f"Paired with {who['name']}." if who else "No pairing request has that code (codes last one hour).")
        return 0 if who else 1
    if not cfg.get("provider") and not os.environ.get("VESPER_FAKE_LLM"):
        note("Pick a model first: run  vesper  once and follow the steps.")
        return 1
    if action == "setup":
        if not tty:
            note("The setup asks you questions; run it in a terminal window:  vesper telegram setup")
            return 1
        return 0 if setup(_ask, _confirm, printer(console), folder) else 1
    if action == "on":
        state = status(check_token=False)
        if not (state["configured"] and state["paired"]):
            note("Set it up first:  vesper telegram setup")
            return 1
        problem = start_service(folder)
        note(problem or "The Telegram bot is on, in the background.")
        return 1 if problem else 0
    # run: in this window (this is also what the background service runs)
    from vesper import providers as prov

    prov.refresh_keys()
    token = os.environ.get(TOKEN_KEY, "").strip()
    if not token:
        note("No Telegram bot yet. Set it up:  vesper telegram setup")
        return 1
    bot = Bot(Api(token), open_store(), cfg, Path(load_settings().get("folder") or folder),
              say=lambda s: print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {s}", flush=True))
    try:
        bot.run()
    except KeyboardInterrupt:
        note("Telegram bot stopped.")
    except TelegramError as e:
        err_console.print(f"{e}\nLog: {log_path()}")
        with contextlib.suppress(OSError):
            save_state(last_error=str(e), last_error_at=time.time())
        if "already running" in str(e):
            return 0  # nothing wrong: the other one answers
        time.sleep(30 if not tty else 0)  # a service that cannot work should not restart in a tight loop
        return 1
    return 0


def _ask(prompt: str, secret: bool = False) -> str | None:
    from prompt_toolkit import prompt as pt_prompt

    try:
        return pt_prompt("  " + prompt, is_password=secret)
    except (KeyboardInterrupt, EOFError):
        return None


def _confirm(prompt: str) -> bool:
    answer = _ask(f"{prompt} [Y/n] ")
    return answer is not None and answer.strip().lower() in ("", "y", "yes")


def watch(console, poll: float = 0.3, stop: threading.Event | None = None) -> None:
    """Show what the bot does, as it happens, until Ctrl+C (or `stop`)."""
    from rich.text import Text

    from vesper import ui

    reader, shown = FeedReader(), ui.FeedPrinter()
    ui.note("Following the Telegram bot: messages, steps and answers show here as they happen. Ctrl+C stops.")
    if not status(check_token=False)["running"]:
        console.print(Text("  The bot is not running right now. Start it with /telegram on.", style=ui.WARN))
    try:
        while stop is None or not stop.is_set():
            for event in reader.read():
                for part in shown.render(event):
                    console.print(part)
            time.sleep(poll)
    except KeyboardInterrupt:
        console.print()


def printer(console):
    """How the setup talks in a terminal: indented like Vesper's other notes, never read as markup."""
    from rich.text import Text

    def say(text: str) -> None:
        console.print(Text(text if text.startswith((" ", "\n")) else "  " + text))

    return say


def show_status(console) -> dict:
    from rich.text import Text

    from vesper import ui

    state = status()
    color = {"ok": ui.OK, "warn": ui.WARN, "off": ui.MUTED}
    console.print(Text.assemble(("  Telegram  ", f"bold {ui.TEXT}"),
                                (state["summary"], ui.OK if state["running"] else ui.WARN)))
    for label, value, kind in state["rows"]:
        console.print(Text.assemble(("    " + label.ljust(13), ui.MUTED), (value, color[kind])))
    for hint in state["hints"]:
        console.print(Text("  " + hint, style=ui.MUTED))
    return state
