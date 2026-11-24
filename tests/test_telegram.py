"""The Telegram bot, offline: a fake Bot API records what Vesper sends and a fake model answers."""

import json
import time

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import telegram, tui
from vesper.store import Store

USER, CHAT = 4242, 4242


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class FakeApi:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    def call(self, method, **params):
        if len(params.get("text", "")) > 4096:  # what Telegram does
            raise telegram.TelegramError("Bad Request: message is too long")
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getMe":
            return {"username": "my_vesper_bot"}
        return True

    def texts(self, method=None):
        return [p.get("text", "") for m, p in self.calls if method in (None, m)]

    def buttons(self):
        return [p["reply_markup"]["inline_keyboard"] for m, p in self.calls
                if m == "sendMessage" and p.get("reply_markup")]


def message(text, user=USER):
    return {"update_id": 1, "message": {"from": {"id": user, "first_name": "Ada"}, "chat": {"id": CHAT}, "text": text}}


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


@pytest.fixture
def cfg_home(home, monkeypatch):
    from vesper import providers

    monkeypatch.setattr(providers, "HOME", home)
    return home


@pytest.fixture
def bot(cfg_home, tmp_path, monkeypatch):
    home = cfg_home
    monkeypatch.setattr(telegram, "EDIT_EVERY_S", 0.01)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hi\n")
    replies = []
    monkeypatch.setattr(tui, "make_llm", lambda cfg: ToolFake(disable_streaming=True, messages=iter(replies)))
    monkeypatch.setattr(tui, "make_small_llm", lambda cfg: None)
    monkeypatch.setattr(tui, "make_switcher", lambda cfg: None)
    (home / "config.json").write_text(json.dumps({"provider": "ollama", "telegram": {"allowed": [USER]}}))
    b = telegram.Bot(FakeApi(), Store(tmp_path / "v.db"), {"provider": "ollama"}, proj, say=lambda s: None)
    b.replies = replies
    return b


def finish_turn(b):
    chat = b.chats[CHAT]
    assert wait(lambda: not chat.busy)


def test_markdown_becomes_telegram_html():
    out = telegram.to_html("# Title\nUse `x < y` and **bold**.\n- one\n```py\nif a < b:\n    pass\n```\n[docs](https://e.x/a)")
    assert "<b>Title</b>" in out and "<code>x &lt; y</code>" in out and "<b>bold</b>" in out
    assert "• one" in out and '<pre><code class="language-py">if a &lt; b:\n    pass</code></pre>' in out
    assert '<a href="https://e.x/a">docs</a>' in out


def test_long_markdown_splits_and_keeps_code_fences_closed():
    md = "intro\n```\n" + "line\n" * 400 + "```\nend\n"
    pieces = telegram.split_md(md, 500)
    assert len(pieces) > 3 and all(len(p) <= 520 for p in pieces)
    assert all(p.count("```") % 2 == 0 for p in pieces)
    assert "".join(pieces).count("line") == 400


def test_pairing_needs_the_code_and_strangers_are_refused(cfg_home, tmp_path):
    api = FakeApi()
    b = telegram.Bot(api, None, {}, tmp_path, say=lambda s: None)
    assert b.code and len(b.code) == 6
    b.handle(message("/start 000000" if b.code != "000000" else "/start 111111", user=7))
    assert "private" in api.texts()[-1] and b.allowed_users() == []
    b.handle(message(f"/start {b.code}", user=7))
    assert b.allowed_users() == [7] and b.code is None and "Paired" in api.texts()[-1]
    b.handle(message("hello", user=8))  # the code works once
    assert "private" in api.texts()[-1] and b.allowed_users() == [7]


def test_a_message_streams_steps_and_the_answer(bot):
    bot.replies += [AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "r1"}]),
                    AIMessage("The file says **hi**.")]
    bot.handle(message("what is in a.txt?"))
    finish_turn(bot)
    sent = "\n".join(bot.api.texts())
    assert "📖" in sent and "a.txt" in sent
    assert "The file says <b>hi</b>." in sent
    assert "◦" in bot.api.texts("sendMessage")[-1]  # the footer closes the turn
    answer = [t for t in bot.api.texts() if "The file says" in t]
    assert "▍" not in answer[-1]  # the last edit of the answer has no cursor


def test_a_command_waits_for_the_yes_button(bot):
    bot.replies += [AIMessage("", tool_calls=[{"name": "run_command", "args": {"command": "echo hello"}, "id": "c1"}]),
                    AIMessage("Done.")]
    bot.handle(message("say hello"))
    assert wait(lambda: bot.approvals)
    aid = next(iter(bot.approvals))
    assert any(f"ap:{aid}:yes" == b["callback_data"] for rows in bot.api.buttons() for row in rows for b in row)
    bot.handle(press(f"ap:{aid}:no", user=999))  # someone else cannot answer
    assert bot.approvals
    bot.handle(press(f"ap:{aid}:yes"))
    finish_turn(bot)
    sent = "\n".join(bot.api.texts())
    assert "Run this command?" in sent and "✅ Allowed" in sent and "hello" in sent and "Done." in sent


def test_resume_lists_conversations_and_replays_one(bot):
    bot.replies += [AIMessage("First answer.")]
    bot.handle(message("first question"))
    finish_turn(bot)
    old = bot.chats[CHAT].session.id
    bot.handle(message("/new"))
    bot.handle(message("/resume"))
    keys = [b["callback_data"] for rows in bot.api.buttons() for row in rows for b in row]
    assert f"rs:{old}" in keys
    bot.handle(press(f"rs:{old}"))
    sent = "\n".join(bot.api.texts())
    assert "Resumed" in sent and "first question" in sent and "First answer." in sent and "you are here" in sent
    assert bot.chats[CHAT].session.id == old


def test_mode_and_folder_are_saved(bot, tmp_path, cfg_home):
    home = cfg_home
    bot.handle(message("/mode"))
    assert any(b["callback_data"] == "md:auto-edit" for rows in bot.api.buttons() for row in rows for b in row)
    bot.handle(press("md:auto-edit"))
    assert bot.chats[CHAT].workspace.mode == "auto-edit"
    other = tmp_path / "other"
    other.mkdir()
    bot.handle(message(f"/cd {other}"))
    saved = json.loads((home / "config.json").read_text())["telegram"]
    assert saved["mode"] == "auto-edit" and saved["folder"] == str(other.resolve())
    assert saved["allowed"] == [USER]


def test_history_messages_stay_under_the_limit():
    from langchain_core.messages import HumanMessage

    msgs = []
    for i in range(30):
        msgs += [HumanMessage(f"question {i}"), AIMessage("answer " * 300)]
    pieces = telegram.history_messages(msgs, max_turns=10)
    assert all(len(p) <= 4096 for p in pieces)
    joined = "\n".join(pieces)
    assert "question 29" in joined and "question 5\n" not in joined and "earlier messages" in joined


def test_background_plist_runs_the_bot_at_login(tmp_path):
    text = telegram.plist_text(["/usr/local/bin/vesper", "telegram", "--folder", "/a & b"], tmp_path / "t.log")
    assert "<string>dev.vesper.telegram</string>" in text and "<key>KeepAlive</key><true/>" in text
    assert "<string>/a &amp; b</string>" in text and "RunAtLoad" in text


def test_a_long_answer_full_of_html_characters_still_arrives(bot):
    code = "```html\n" + "<div class=\"a&b\"></div>\n" * 400 + "```\nThat is the page."
    bot.replies += [AIMessage(code)]
    bot.handle(message("write a page"))
    finish_turn(bot)
    sent = bot.api.texts()
    assert all(len(t) <= 4096 for t in sent)
    assert "That is the page." in "\n".join(sent) and "◦" in sent[-1]
    assert sum(t.count("&lt;div") for t in bot.api.texts("sendMessage")) >= 1


def test_a_refused_message_does_not_stop_the_agent(bot, monkeypatch):
    bot.replies += [AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "r1"}]),
                    AIMessage("Still here.")]
    real = bot.api.call

    def flaky(method, **params):
        if method == "editMessageText":
            raise telegram.TelegramError("Bad Request: message to edit not found")
        return real(method, **params)

    monkeypatch.setattr(bot.api, "call", flaky)
    bot.handle(message("read it"))
    finish_turn(bot)
    assert "Still here." in "\n".join(bot.api.texts()) and "◦" in bot.api.texts()[-1]


def test_guessing_the_pairing_code_disables_it(cfg_home, tmp_path):
    api = FakeApi()
    b = telegram.Bot(api, None, {}, tmp_path, say=lambda s: None)
    code = b.code
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(telegram.MAX_WRONG_CODES):
        b.handle(message(f"/start {wrong}", user=9))
    b.handle(message(f"/start {code}", user=9))
    assert b.code is None and b.allowed_users() == []


def test_vesper_never_kills_itself():
    import os

    from vesper.workspace import stops_vesper

    assert stops_vesper('pkill -f "vesper telegram"') and stops_vesper("killall vesper")
    assert stops_vesper(f"kill -9 {os.getpid()}") and stops_vesper(f"sleep 1; kill {os.getpid()} 2>/dev/null")
    assert not stops_vesper("kill -9 1") and not stops_vesper(f"kill {os.getpid()}0") and not stops_vesper("ps aux | grep vesper")


def test_typing_vesper_telegram_inside_vesper_does_not_reach_the_model(monkeypatch):
    app = tui.App.__new__(tui.App)
    seen = []
    monkeypatch.setattr(tui.App, "telegram", lambda self, arg: seen.append(arg))
    app.vesper_command("vesper telegram --stop")
    app.vesper_command("vesper telegram")
    app.vesper_command("vesper telegram --background")
    assert seen == ["off", "", "on"]


def test_status_explains_each_stage(cfg_home, monkeypatch):
    monkeypatch.delenv(telegram.TOKEN_KEY, raising=False)
    monkeypatch.setattr(telegram, "running_in_background", lambda: False)
    from vesper import providers

    monkeypatch.setattr(providers, "refresh_keys", lambda: None)
    assert telegram.status()["text"] == "not set up yet"
    monkeypatch.setenv(telegram.TOKEN_KEY, "1:x")
    assert telegram.status()["text"] == "set up, not paired yet"
    (cfg_home / "config.json").write_text(json.dumps({"telegram": {"allowed": [1]}}))
    assert telegram.status()["text"] == "set up and paired, not running"
    monkeypatch.setattr(telegram, "running_in_background", lambda: True)
    assert telegram.status()["running"]
