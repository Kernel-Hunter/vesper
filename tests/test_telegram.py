"""The Telegram bot, offline. A fake Bot API applies Telegram's own rules (its HTML parser, its
length limit) so a message the real Telegram would refuse fails here too; a fake model answers."""

import html as htmllib
import json
import os
import re
import threading
import time

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from vesper import telegram, tui
from vesper.store import Store

USER, CHAT = 4242, 4242
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a", "code",
                "pre", "blockquote", "tg-emoji"}


def telegram_parse(text: str) -> str:
    """Telegram's HTML rules, written down independently of the code under test. Returns the
    plain text Telegram would show, or raises the error Telegram would give."""
    stack = []
    for m in re.finditer(r"<(/?)([^\s>/]+)([^>]*)>|&([^;\s]*);?|<", text):
        if m.group(0) == "<":
            raise telegram.TelegramError("Bad Request: can't parse entities: unexpected <", 400)
        if m.group(4) is not None:
            if not re.fullmatch(r"lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+", m.group(4)) or not m.group(0).endswith(";"):
                raise telegram.TelegramError("Bad Request: can't parse entities: unsupported entity", 400)
            continue
        closing, tag = m.group(1), m.group(2)
        if tag not in ALLOWED_TAGS:
            raise telegram.TelegramError(f"Bad Request: can't parse entities: unsupported start tag \"{tag}\"", 400)
        if closing:
            if not stack or stack.pop() != tag:
                raise telegram.TelegramError("Bad Request: can't parse entities: unmatched end tag", 400)
        else:
            if "code" in stack or ("pre" in stack and tag != "code") or (tag == "blockquote" and "blockquote" in stack):
                raise telegram.TelegramError("Bad Request: can't parse entities: wrong nesting", 400)
            stack.append(tag)
    if stack:
        raise telegram.TelegramError("Bad Request: can't parse entities: unclosed tag", 400)
    return htmllib.unescape(re.sub(r"<[^>]+>", "", text))


class FakeApi:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.updates = []
        self.lock = threading.Lock()

    def call(self, method, **params):
        if method in ("sendMessage", "editMessageText"):
            text = params.get("text", "")
            shown = telegram_parse(text) if params.get("parse_mode") == "HTML" else text
            if not shown.strip():
                raise telegram.TelegramError("Bad Request: message text is empty", 400)
            if len(shown.encode("utf-16-le")) // 2 > 4096:
                raise telegram.TelegramError("Bad Request: message is too long", 400)
        with self.lock:
            self.calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getMe":
            return {"username": "my_vesper_bot"}
        if method == "getUpdates":
            if params.get("offset") == -1:
                return self.updates[-1:]
            out = [u for u in self.updates if u["update_id"] >= (params.get("offset") or 0)]
            if not out and params.get("timeout"):
                time.sleep(0.05)
            return out
        return True

    def texts(self, method=None):
        return [p.get("text", "") for m, p in self.calls if method in (None, m)]

    def buttons(self):
        return [p["reply_markup"]["inline_keyboard"] for m, p in self.calls
                if m == "sendMessage" and p.get("reply_markup")]

    def keys(self):
        return [b["callback_data"] for rows in self.buttons() for row in rows for b in row]


def message(text, user=USER, date=None, **extra):
    return {"update_id": 1, "message": {"message_id": 7, "from": {"id": user, "first_name": "Ada"},
                                        "chat": {"id": CHAT}, "text": text, "date": int(date or time.time()), **extra}}


def press(data, user=USER):
    return {"update_id": 2, "callback_query": {"id": "q", "from": {"id": user}, "data": data,
                                               "message": {"chat": {"id": CHAT}}}}


def wait(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def cfg_home(home, monkeypatch):
    from vesper import providers

    monkeypatch.setattr(providers, "HOME", home)
    return home


def pair(user=USER, name="Ada"):
    telegram.update_settings(lambda d: (d["allowed"].append(user), d["names"].update({str(user): name})))


@pytest.fixture
def bot(cfg_home, tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "EDIT_EVERY_S", 0.01)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hi\n")
    replies = []
    monkeypatch.setattr(tui, "make_llm", lambda cfg: ToolFake(disable_streaming=True, messages=iter(replies)))
    monkeypatch.setattr(tui, "make_small_llm", lambda cfg: None)
    monkeypatch.setattr(tui, "make_switcher", lambda cfg: None)
    pair()
    b = telegram.Bot(FakeApi(), Store(tmp_path / "v.db"), {"provider": "ollama"}, proj, say=lambda s: None)
    b.replies = replies
    return b


def finish_turn(b):
    assert wait(lambda: CHAT in b.chats and not b.chats[CHAT].busy)


# ---------------------------------------------------------------- formatting
def test_markdown_becomes_html_telegram_accepts():
    out = telegram.to_html("# Title\nUse `x < y` and **bold**.\n- one\n```py\nif a < b:\n    pass\n```\n[docs](https://e.x/a)")
    telegram_parse(out)
    assert "<b>Title</b>" in out and "<code>x &lt; y</code>" in out and "<b>bold</b>" in out
    assert "• one" in out and '<pre><code class="language-py">if a &lt; b:\n    pass</code></pre>' in out
    assert '<a href="https://e.x/a">docs</a>' in out


@pytest.mark.parametrize("md", [
    "**a `b** c`", "**bold `code** more`", "# **head** with `x`", "```\nunclosed fence <b>", "a & b <c> **",
    "[x](https://a.b/?q=\"1\")", "**a\n**b", "`a` **`b`** `c`", "<script>alert(1)</script>", "🙂 **😀**" * 50,
])
def test_any_markdown_gives_html_telegram_accepts(md):
    telegram_parse(telegram.to_html(md))


def test_long_markdown_splits_and_keeps_code_fences_closed():
    md = "intro\n```\n" + "line\n" * 400 + "```\nend\n"
    pieces = telegram.split_md(md, 500)
    assert len(pieces) > 3 and all(len(p) <= 520 for p in pieces)
    assert all(p.count("```") % 2 == 0 for p in pieces)
    assert "".join(pieces).count("line") == 400


def test_lengths_are_counted_like_telegram():
    assert telegram.units("😀") == 2 and telegram.units("é") == 1
    long = "😀" * 3000
    assert telegram.units(telegram.clip(long)) <= 4000


# ---------------------------------------------------------------- a turn, live
def test_a_message_streams_steps_and_the_answer(bot):
    bot.replies += [AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "r1"}]),
                    AIMessage("The file says **hi**.")]
    bot.handle(message("what is in a.txt?"))
    finish_turn(bot)
    sent = "\n".join(bot.api.texts())
    assert "📖" in sent and "a.txt" in sent and "The file says <b>hi</b>." in sent
    methods = [m for m, _ in bot.api.calls]
    assert "setMessageReaction" in methods and "sendChatAction" in methods  # 👀 and typing…
    last = [p for m, p in bot.api.calls if m == "sendMessage"][-1]
    assert "◦" in last["text"] and last["disable_notification"] is False  # only the end buzzes
    answer = [t for t in bot.api.texts() if "The file says" in t]
    assert "▍" not in answer[-1]


def test_a_command_waits_for_the_yes_button(bot):
    bot.replies += [AIMessage("", tool_calls=[{"name": "run_command", "args": {"command": "echo hello"}, "id": "c1"}]),
                    AIMessage("Done.")]
    bot.handle(message("say hello"))
    assert wait(lambda: bot.approvals)
    aid = next(iter(bot.approvals))
    assert f"ap:{aid}:yes" in bot.api.keys()
    bot.handle(press(f"ap:{aid}:no", user=999))  # someone else cannot answer
    assert bot.approvals
    bot.handle(press(f"ap:{aid}:yes"))
    finish_turn(bot)
    sent = "\n".join(bot.api.texts())
    assert "Run this command?" in sent and "✅ Allowed" in sent and "hello" in sent and "Done." in sent


def test_typing_yes_answers_the_open_question(bot):
    bot.replies += [AIMessage("", tool_calls=[{"name": "write_file", "args": {"path": "b.txt", "content": "x\n"},
                                               "id": "w1"}]), AIMessage("Written.")]
    bot.handle(message("make b.txt"))
    assert wait(lambda: bot.approvals)
    bot.handle(message("Yes"))
    finish_turn(bot)
    assert (bot.chats[CHAT].workspace.root / "b.txt").read_text() == "x\n"


def test_a_long_answer_full_of_html_characters_still_arrives(bot):
    code = "```html\n" + "<div class=\"a&b\"></div>\n" * 400 + "```\nThat is the page."
    bot.replies += [AIMessage(code)]
    bot.handle(message("write a page"))
    finish_turn(bot)
    sent = bot.api.texts()
    assert "That is the page." in "\n".join(sent) and "◦" in sent[-1]
    assert sum(t.count("&lt;div") for t in bot.api.texts("sendMessage")) >= 400


def test_a_refused_message_does_not_stop_the_agent(bot, monkeypatch):
    bot.replies += [AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "r1"}]),
                    AIMessage("Still here.")]
    real = bot.api.call

    def flaky(method, **params):
        if method == "editMessageText":
            raise telegram.TelegramError("Bad Request: message to edit not found", 400)
        return real(method, **params)

    monkeypatch.setattr(bot.api, "call", flaky)
    bot.handle(message("read it"))
    finish_turn(bot)
    assert "Still here." in "\n".join(bot.api.texts()) and "◦" in bot.api.texts()[-1]


def test_photos_reach_the_model_and_files_land_in_the_inbox(bot, monkeypatch):
    monkeypatch.setattr(bot.api, "download", lambda fid, **k: (b"\xff\xd8jpeg" if fid == "p" else b"col\n1\n",
                                                               "photos/x.jpg" if fid == "p" else "docs/y"), raising=False)
    seen = []
    monkeypatch.setattr(bot, "run_turn", lambda chat, text, images=None: seen.append((text, images)))
    bot.handle(message("", photo=[{"file_id": "small"}, {"file_id": "p"}], caption="what is this?"))
    assert seen[-1][0] == "what is this?" and seen[-1][1][0]["mime_type"] == "image/jpeg"
    bot.handle(message("", document={"file_id": "d", "file_name": "data.csv"}))
    text = seen[-1][0]
    path = re.search(r"saved at (\S+)\]", text).group(1)
    assert open(path).read() == "col\n1\n" and "/telegram/inbox/" in path


def test_messages_sent_while_the_bot_was_off_are_not_run(bot):
    bot.handle(message("delete everything", date=time.time() - 3600))
    assert "offline" in bot.api.texts()[-1] and CHAT not in bot.chats


# ---------------------------------------------------------------- pairing and access
def test_strangers_get_a_code_the_owner_approves(cfg_home, tmp_path):
    api = FakeApi()
    b = telegram.Bot(api, None, {}, tmp_path, say=lambda s: None)
    b.handle(message("hello", user=7))
    code = re.search(r"/telegram approve ([A-Z2-9]{8})", api.texts()[-1]).group(1)
    assert not set(code) & set("01OI") and telegram.allowed_users() == []
    b.handle(message("hello again", user=7))  # the same person keeps the same code
    assert code in api.texts()[-1]
    for other in (8, 9, 10):
        b.handle(message("hi", user=other))
    assert "approve" not in api.texts()[-1]  # three pending at most
    assert telegram.approve_code("WRONG123") is None
    who = telegram.approve_code(code.lower())
    assert who["user"] == 7 and telegram.allowed_users() == [7]
    assert telegram.approve_code(code) is None  # a code works once


def test_buttons_from_strangers_do_nothing(bot):
    bot.handle(press("md:yolo", user=999))
    assert CHAT not in bot.chats or bot.chats[CHAT].workspace.mode == "ask"


def test_old_settings_in_config_json_are_kept(cfg_home):
    (cfg_home / "config.json").write_text(json.dumps({"telegram": {"allowed": [5], "mode": "auto-edit"}}))
    assert telegram.allowed_users() == [5] and telegram.load_settings()["mode"] == "auto-edit"


def test_mode_and_folder_are_saved(bot, tmp_path):
    bot.handle(message("/mode"))
    assert "md:auto-edit" in bot.api.keys()
    bot.handle(press("md:auto-edit"))
    assert bot.chats[CHAT].workspace.mode == "auto-edit"
    other = tmp_path / "other"
    other.mkdir()
    bot.handle(message(f"/cd {other}"))
    saved = telegram.load_settings()
    assert saved["mode"] == "auto-edit" and saved["folder"] == str(other.resolve()) and saved["allowed"] == [USER]


def test_resume_lists_conversations_and_replays_one(bot):
    bot.replies += [AIMessage("First answer.")]
    bot.handle(message("first question"))
    finish_turn(bot)
    old = bot.chats[CHAT].session.id
    bot.handle(message("/new"))
    bot.handle(message("/resume"))
    assert f"rs:{old}" in bot.api.keys()
    bot.handle(press(f"rs:{old}"))
    sent = "\n".join(bot.api.texts())
    assert "Resumed" in sent and "first question" in sent and "First answer." in sent and "you are here" in sent
    assert bot.chats[CHAT].session.id == old


def test_history_messages_stay_under_the_limit():
    msgs = []
    for i in range(30):
        msgs += [HumanMessage(f"question {i}"), AIMessage("answer <x> & " * 300)]
    pieces = telegram.history_messages(msgs, max_turns=10)
    for p in pieces:
        telegram_parse(p)
    assert all(telegram.units(p) <= 4096 for p in pieces)
    joined = "\n".join(pieces)
    assert "question 29" in joined and "question 5\n" not in joined and "earlier messages" in joined


# ---------------------------------------------------------------- the poller: one at a time, no repeats
def test_only_one_poller_at_a_time(cfg_home):
    first = telegram.PollerLock()
    first.acquire()
    try:
        assert telegram.lock_holder() == os.getpid()
        with pytest.raises(telegram.AlreadyRunning):
            telegram.PollerLock().acquire()
    finally:
        first.release()
    assert telegram.lock_holder() is None


def test_the_offset_survives_a_restart_so_nothing_runs_twice(bot, monkeypatch):
    seen = []
    monkeypatch.setattr(bot, "handle", lambda u: seen.append(u["update_id"]))
    bot.api.updates = [{"update_id": 10, "message": {}}, {"update_id": 11, "message": {}}]
    stop = threading.Event()
    t = threading.Thread(target=bot.run, args=(stop,))
    t.start()
    assert wait(lambda: len(seen) >= 2)
    stop.set()
    t.join(5)
    assert telegram.load_state()["offset"] == 12
    again = telegram.Bot(bot.api, None, {}, bot.folder, say=lambda s: None)
    monkeypatch.setattr(again, "handle", lambda u: seen.append(u["update_id"]))
    stop = threading.Event()
    t = threading.Thread(target=again.run, args=(stop,))
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(5)
    assert seen == [10, 11]


def test_a_second_bot_says_it_is_already_running(bot):
    lock = telegram.PollerLock()
    lock.acquire()
    try:
        with pytest.raises(telegram.TelegramError, match="already running"):
            bot.run(threading.Event())
    finally:
        lock.release()


def test_conflict_waits_then_explains(bot, monkeypatch):
    monkeypatch.setattr(telegram, "CONFLICT_WAITS", (0, 0))
    real = bot.api.call

    def conflict(method, **params):
        if method == "getUpdates":
            raise telegram.TelegramError("Conflict: terminated by other getUpdates request", 409)
        return real(method, **params)

    monkeypatch.setattr(bot.api, "call", conflict)
    with pytest.raises(telegram.TelegramError, match="Only one can"):
        bot.run()
    assert telegram.lock_holder() is None


# ---------------------------------------------------------------- setup and health
def test_setup_pairs_the_owner_and_starts_the_service(cfg_home, monkeypatch, tmp_path):
    api = FakeApi()
    api.updates = [{"update_id": 5, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "old"}}]
    monkeypatch.setattr(telegram, "Api", lambda token: api)
    monkeypatch.setattr(telegram, "stop_service", lambda: None)
    started = []
    monkeypatch.setattr(telegram, "start_service", lambda folder: started.append(folder))
    saved = {}
    from vesper import providers

    monkeypatch.setattr(providers, "save_user_settings", lambda v: saved.update(v))
    monkeypatch.setattr(providers, "refresh_keys", lambda: None)
    monkeypatch.delenv(telegram.TOKEN_KEY, raising=False)
    token = "123456:" + "A" * 35
    answers = iter(["not a token", token])
    out = []

    def confirm(prompt):
        out.append(prompt)
        return True

    def later():
        time.sleep(0.2)
        api.updates.append({"update_id": 6, "message": {"from": {"id": 77, "first_name": "Karim", "username": "k"},
                                                        "chat": {"id": 77}, "text": "/start"}})

    threading.Thread(target=later).start()
    assert telegram.setup(lambda p, s=False: next(answers), confirm, out.append, tmp_path)
    assert saved[telegram.TOKEN_KEY] == token and started == [tmp_path]
    assert telegram.allowed_users() == [77] and "Karim (@k) wrote to the bot. Is that you?" in out
    assert any("does not look like a bot token" in str(o) for o in out)
    assert telegram.load_state()["offset"] == 7  # the /start is not run as a task later


def test_status_explains_each_stage(cfg_home, monkeypatch):
    from vesper import providers

    monkeypatch.setattr(providers, "refresh_keys", lambda: None)
    monkeypatch.setattr(telegram, "service_running", lambda: False)
    monkeypatch.delenv(telegram.TOKEN_KEY, raising=False)
    assert telegram.status(False)["summary"] == "not set up yet"
    monkeypatch.setenv(telegram.TOKEN_KEY, "1:x")
    assert telegram.status(False)["summary"] == "set up, not paired yet"
    pair(1, "Karim")
    s = telegram.status(False)
    assert s["summary"] == "set up, not running" and ("Paired", "Karim", "ok") in s["rows"]
    lock = telegram.PollerLock()
    lock.acquire()
    telegram.save_state(heartbeat=time.time(), username="kb")
    try:
        s = telegram.status(False)
        assert s["running"] and s["summary"] == "running as @kb"
        assert any("awake" in h for h in s["hints"])
    finally:
        lock.release()


def test_service_files_run_the_bot_at_login(tmp_path):
    args = ["/usr/local/bin/vesper", "telegram", "run", "--folder", "/a & b"]
    text = telegram.plist_text(args, tmp_path / "t.log")
    assert "<string>dev.vesper.telegram</string>" in text and "<key>KeepAlive</key><true/>" in text
    assert "<string>/a &amp; b</string>" in text and "<string>run</string>" in text
    unit = telegram.unit_text(args, tmp_path / "t.log")
    assert "Restart=always" in unit and "telegram run" in unit


# ---------------------------------------------------------------- Vesper never trips over itself
def test_vesper_never_kills_or_nests_itself():
    from vesper.workspace import starts_vesper, stops_vesper

    assert stops_vesper('pkill -f "vesper telegram"') and stops_vesper("killall vesper")
    assert stops_vesper(f"kill -9 {os.getpid()}") and stops_vesper(f"sleep 1; kill {os.getpid()} 2>/dev/null")
    assert not stops_vesper("kill -9 1") and not stops_vesper(f"kill {os.getpid()}0")
    assert not stops_vesper("ps aux | grep vesper")
    for cmd in ("vesper", "vesper telegram", "vesper telegram run", "cd x && vesper telegram setup",
                "/usr/local/bin/vesper -C ~/x"):
        assert starts_vesper(cmd), cmd
    for cmd in ("vesper --version", "vesper telegram status", "vesper telegram off", "vesper sessions",
                'vesper "list files"', "grep vesper x.txt", "pip install vesper-agent"):
        assert not starts_vesper(cmd), cmd


def test_typing_vesper_telegram_inside_vesper_does_not_reach_the_model(monkeypatch):
    app = tui.App.__new__(tui.App)
    seen = []
    monkeypatch.setattr(tui.App, "telegram", lambda self, arg: seen.append(arg))
    for line in ("vesper telegram --stop", "vesper telegram", "vesper telegram --background", "vesper telegram setup"):
        app.vesper_command(line)
    assert seen == ["off", "", "on", "setup"]
