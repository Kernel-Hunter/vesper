"""Saved history (SQLite), resuming, the activity log and the terminal commands around them. Offline."""

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vesper import prompts
from vesper.coder import Session, fit_history, repair_history, transcript
from vesper.store import Store
from vesper.workspace import Workspace


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def fake(*replies):
    return ToolFake(disable_streaming=True, messages=iter(replies))


def call(name, call_id="c1", **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "vesper.db")
    yield s
    s.close()


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha\n")
    return root


# ---------------------------------------------------------------- the store
def test_sidebar_upgrade_preserves_existing_history(tmp_path):
    path = tmp_path / "old.db"
    old = Store(path)
    sid = old.create_session(tmp_path, title="Existing")
    old.save_messages(sid, [HumanMessage("hello"), AIMessage("hi")])
    old.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE session_sidebar")
        db.execute("PRAGMA user_version=2")
    upgraded = Store(path)
    try:
        assert [m.content for m in upgraded.load_messages(sid)] == ["hello", "hi"]
        row = upgraded.list_sessions()[0]
        assert row["id"] == sid and not row["pinned"] and not row["archived"]
        upgraded.update_sidebar(sid, {"pinned": True})
        assert upgraded.list_sessions()[0]["pinned"] == 1
    finally:
        upgraded.close()


def test_store_roundtrip(store, tmp_path):
    sid = store.create_session(tmp_path, "ollama", "qwen", "terminal", title="first  question\nhere")
    assert store.list_sessions() == []  # no messages yet: not listed
    messages = [HumanMessage("hi"), call("read_file", path="a.txt"),
                ToolMessage("alpha", tool_call_id="c1", name="read_file"), AIMessage("It says alpha.")]
    store.save_messages(sid, messages)
    store.save_messages(sid, messages)  # saving again replaces, never duplicates
    back = store.load_messages(sid)
    assert [m.type for m in back] == ["human", "ai", "tool", "ai"]
    assert back[1].tool_calls[0]["args"] == {"path": "a.txt"} and back[2].tool_call_id == "c1"
    row = store.list_sessions()[0]
    assert row["messages"] == 4 and row["cwd"] == str(tmp_path) and row["model"] == "qwen"
    assert store.get_session(sid[:5])["id"] == sid and store.get_session("zzz") is None
    assert store.latest(tmp_path)["id"] == sid and store.latest(tmp_path / "other") is None
    store.update_session(sid, cwd=tmp_path / "other", title="renamed")
    assert store.latest(tmp_path / "other")["title"] == "renamed"
    store.add_event(sid, "tool", "read_file", {"args": {"path": "a.txt"}, "output": "x" * 50_000}, ok=True, ms=12)
    event = store.events(sid)[0]
    assert event["ok"] is True and event["ms"] == 12 and len(event["detail"]["output"]) < 21_000
    store.delete_session(sid)
    assert store.get_session(sid) is None and store.events(sid) == [] and store.load_messages(sid) == []


def test_search_sessions_matches_saved_conversation_text_without_tool_results(store, tmp_path):
    first = store.create_session(tmp_path / "alpha", title="First")
    second = store.create_session(tmp_path / "beta", title="Second")
    store.save_messages(first, [HumanMessage("Find the unusual zipper"), AIMessage("Solved 100% of it")])
    store.save_messages(second, [HumanMessage("Other question"), call("read_file"),
                                 ToolMessage("hidden tool output", tool_call_id="c1")])
    ids = lambda q: {r["id"] for r in store.list_sessions(200, query=q)}  # noqa: E731
    assert ids("ZIPPER") == {first}
    assert ids("100%") == {first}
    assert ids("%") == {first}  # not a SQL wildcard
    assert ids("second") == {second}
    assert ids("alpha") == {first}
    assert ids("hidden tool output") == set()
    assert store.list_sessions(200, cwd=tmp_path / "beta", query="zipper") == []


def test_store_is_a_plain_sqlite_file(store, tmp_path):
    store.create_session(tmp_path)
    tables = {r[0] for r in sqlite3.connect(store.path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "messages", "events"} <= tables


# ---------------------------------------------------------------- sessions
def test_session_is_saved_logged_and_resumed(store, proj):
    ws = Workspace(proj, mode="yolo")
    s = Session(fake(call("read_file", path="a.txt"), AIMessage("It says alpha.")), ws, store,
                provider="ollama", model="qwen")
    events = list(s.ask("what is in a.txt?"))
    kinds = [e for e, _ in events]
    assert kinds[-2:] == ["usage", "done"]
    usage = dict(events)["usage"]
    assert usage["tools"] == 1 and usage["output"] > 0 and usage["seconds"] >= 0
    end = next(d for e, d in events if e == "tool_end")
    assert end["ok"] and end["ms"] is not None

    row = store.get_session(s.id)
    assert row["title"] == "what is in a.txt?" and row["cwd"] == str(proj.resolve())
    log = store.events(s.id)
    assert [e["kind"] for e in log] == ["prompt", "tool", "usage"]
    assert log[1]["name"] == "read_file" and log[1]["detail"]["args"] == {"path": "a.txt"}
    assert "alpha" in log[1]["detail"]["output"]

    again = Session(fake(AIMessage("Yes.")), Workspace(proj), store, session_id=s.id[:6])
    assert again.id == s.id and len(again.history) == len(s.history) == 4
    list(again.ask("sure?"))
    assert len(store.load_messages(s.id)) == 6

    s.clear()
    assert s.history == [] and s.id != row["id"] and store.get_session(row["id"]) is not None


def test_openai_response_stream_is_not_repeated(proj):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model="offline-test", api_key="offline-test", use_responses_api=True)
    response = [
        SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_test")),
        SimpleNamespace(type="response.output_text.delta", output_index=0, content_index=0, delta="Hey! "),
        SimpleNamespace(type="response.output_text.delta", output_index=0, content_index=0,
                        delta="What can I help you with?"),
        SimpleNamespace(type="response.output_text.done", output_index=0, content_index=0, item_id="msg_test"),
    ]
    stream = MagicMock()
    stream.__enter__.return_value = iter(response)
    llm.root_client.responses.create = MagicMock(return_value=stream)
    s = Session(llm, Workspace(proj))
    events = list(s.ask("yo"))
    assert "".join(d["text"] for e, d in events if e == "token") == "Hey! What can I help you with?"


def test_stream_deduplication_is_scoped_to_each_model_step(proj, monkeypatch):
    from langchain_core.messages import AIMessageChunk

    class Agent:
        def stream(self, *args, **kwargs):
            yield "messages", (AIMessageChunk("First.", id="chunk-1"), {"langgraph_node": "model"})
            yield "updates", {"model": {"messages": [AIMessage("First.", id="response-1")]}}
            yield "updates", {"model": {"messages": [AIMessage("Second.", id="response-2")]}}
            yield "messages", (AIMessageChunk("Third.", id="chunk-3"), {"langgraph_node": "model"})
            yield "updates", {"model": {"messages": [AIMessage("Third.", id="response-3")]}}
            yield "messages", (AIMessageChunk("", id="chunk-4", additional_kwargs={"reasoning_content": "Thinking."}),
                               {"langgraph_node": "model"})
            yield "updates", {"model": {"messages": [AIMessage("Fourth.", id="response-4")]}}

    s = Session(fake(), Workspace(proj))
    monkeypatch.setattr(s, "_agent_for_cwd", lambda: Agent())
    events = list(s.ask("test"))
    assert [d["text"] for e, d in events if e == "token"] == ["First.", "Second.", "Third.", "Fourth."]
    assert [d["text"] for e, d in events if e == "reasoning"] == ["Thinking."]


def test_agent_can_move_between_folders(store, proj):
    ws = Workspace(proj, mode="yolo")
    s = Session(fake(call("change_directory", path="sub"), call("write_file", "c2", path="note.txt", content="hi\n"),
                     AIMessage("Done.")), ws, store)
    events = list(s.ask("go into sub and write a note"))
    assert ("cwd", {"path": str((proj / "sub").resolve())}) in events
    assert (proj / "sub" / "note.txt").read_text() == "hi\n"
    assert store.get_session(s.id)["cwd"] == str((proj / "sub").resolve())
    assert "cwd" in [e["kind"] for e in store.events(s.id)]
    # The next turn's system prompt names the new folder.
    s.set_llm(fake(AIMessage("ok")))
    list(s.ask("and now?"))
    assert s._agent_key[0] == (proj / "sub").resolve()


def test_failed_turn_is_logged_and_history_stays_usable(store, proj):
    class Broken(ToolFake):
        def _generate(self, *a, **k):
            raise RuntimeError("model exploded")

    s = Session(Broken(messages=iter([])), Workspace(proj), store)
    with pytest.raises(RuntimeError):
        list(s.ask("hello"))
    assert [e["kind"] for e in store.events(s.id)] == ["prompt", "error"]
    s.set_llm(fake(AIMessage("fine now")))
    assert "fine now" in "".join(d.get("text", "") for e, d in s.ask("again") if e == "token")


def test_stopped_turn_keeps_a_valid_history(store, proj):
    s = Session(fake(call("read_file", path="a.txt"), AIMessage("never reached")), Workspace(proj), store)
    gen = s.ask("read it")
    for event, _ in gen:
        if event == "tool_start":
            gen.close()  # the user pressed Ctrl+C
            break
    assert s.history and isinstance(s.history[0], HumanMessage)
    answered = {m.tool_call_id for m in s.history if isinstance(m, ToolMessage)}
    assert all(c["id"] in answered for m in s.history if isinstance(m, AIMessage) for c in m.tool_calls)
    assert "interrupt" in [e["kind"] for e in store.events(s.id)]


def test_repair_and_fit_history():
    broken = [AIMessage("stray"), HumanMessage("q"), call("grep", "x1", pattern="a")]
    fixed = repair_history(broken)
    assert isinstance(fixed[0], HumanMessage) and isinstance(fixed[-1], ToolMessage) and fixed[-1].tool_call_id == "x1"

    big = "y" * 5000
    history = []
    for i in range(6):
        history += [HumanMessage(f"q{i}"), call("read_file", f"c{i}", path=f"f{i}"),
                    ToolMessage(big, tool_call_id=f"c{i}", name="read_file"), AIMessage(f"a{i}")]
    same, cut = fit_history(history, 10**9)
    assert same is history and not cut
    trimmed, cut = fit_history(history, 12_000)
    assert cut and len(trimmed) == len(history)             # old tool output shortened, nothing dropped
    assert len(trimmed[2].content) < 2000 and trimmed[-2].content == big  # the latest turn is untouched
    small, cut = fit_history(history, 6_000)
    assert cut and isinstance(small[0], HumanMessage) and small[-1].content == "a5" and len(small) < len(history)

    # The same file read again and again: only the last result is kept, and that is enough.
    again = []
    for i in range(6):
        again += [HumanMessage(f"q{i}"), call("read_file", f"c{i}", path="f"),
                  ToolMessage(big, tool_call_id=f"c{i}", name="read_file"), AIMessage(f"a{i}")]
    deduped, cut = fit_history(again, 12_000)
    assert cut and len(deduped) == len(again) and deduped[-2].content == big
    assert all("read again later" in m.content for m in deduped[:-2] if isinstance(m, ToolMessage))


def test_long_conversation_is_trimmed_with_a_notice(proj):
    # The model that cannot summarize (it fails): older turns are dropped instead.
    class Broken(ToolFake):
        def _generate(self, messages, *args, **kwargs):
            if messages[0].content == prompts.COMPACT_PROMPT:
                raise RuntimeError("no summary today")
            return super()._generate(messages, *args, **kwargs)

    s = Session(Broken(disable_streaming=True, messages=iter([AIMessage("ok"), AIMessage("ok 2")])), Workspace(proj),
                context_tokens=100)
    s.history = [HumanMessage("old " * 300), AIMessage("old answer " * 300), HumanMessage("b"), AIMessage("c")]
    sent = []
    real = s._agent_for_cwd

    def spy():
        agent = real()
        stream = agent.stream
        agent_stream = lambda inputs, *a, **k: (sent.append(inputs["messages"]), stream(inputs, *a, **k))[1]  # noqa: E731
        return type("A", (), {"stream": staticmethod(agent_stream)})()

    s._agent_for_cwd = spy
    notices = [d["text"] for e, d in s.ask("next") if e == "notice"]
    assert notices and "trimmed" in notices[0]
    assert sent[0][0].content == "b"  # the model got the part that fits
    assert s.history[0].content.startswith("old") and s.history[-1].content == "ok"  # nothing is lost from the saved chat
    assert [text for e, d in s.ask("again") if e == "notice" for text in [d["text"]]] == []  # said once per session


# ---------------------------------------------------------------- prompts
def test_system_prompt_is_general_and_reads_project_instructions(proj):
    text = prompts.agent_system_prompt(proj)
    assert str(proj) in text and "change_directory" in text and "Project instructions" not in text
    for word in ("MEDTECH", "handbook", "help desk"):
        assert word.lower() not in text.lower()
    (proj / "AGENTS.md").write_text("Use tabs.\n")
    assert "Project instructions (AGENTS.md)" in prompts.agent_system_prompt(proj)
    (proj / "VESPER.md").write_text("Use spaces.\n")
    assert "Use spaces." in prompts.agent_system_prompt(proj) and "Use tabs." not in prompts.agent_system_prompt(proj)


# ---------------------------------------------------------------- terminal app
@pytest.fixture
def app(proj, store, monkeypatch):
    from vesper import tui

    monkeypatch.setenv("VESPER_FAKE_LLM", "1")
    monkeypatch.setattr(tui, "save_config", lambda patch: None)
    a = tui.App(proj, "yolo", {}, store)
    a.build()
    return a


def test_terminal_turn_cd_sessions_logs_and_resume(app, proj, store, capsys):
    app.run_turn("make notes", shown="make notes")
    assert (proj / "vesper-notes.md").exists()
    out = capsys.readouterr().out
    assert "List" in out and "Write" in out and "tool call" in out and "Created vesper-notes.md" in out

    assert app.command("/cd sub") and app.root == (proj / "sub").resolve()
    app.shell("cd ..")
    assert app.root == proj.resolve()
    app.command("/cd nowhere")
    assert "does not exist" in capsys.readouterr().out and app.root == proj.resolve()

    for cmd in ("/sessions", "/logs", "/logs full", "/status", "/help", "/verbose", "/verbose", "/transcript", "/bogus"):
        assert app.command(cmd)
    out = capsys.readouterr().out
    assert "make notes" in out and "vesper-notes.md" in out and "Unknown command /bogus" in out

    first = app.session.id
    app.command("/new")
    assert app.session.id != first and app.session.history == []
    app.cd("sub")
    app.resume("1")
    assert app.session.id == first and len(app.session.history) > 0 and app.root == proj.resolve()
    assert "Resumed" in capsys.readouterr().out
    assert app.command("/exit") is False


def test_cli_sessions_logs_and_continue(proj, monkeypatch, capsys):
    from vesper import tui

    monkeypatch.setenv("VESPER_FAKE_LLM", "1")
    monkeypatch.setattr(tui, "load_config", lambda: {})
    monkeypatch.setattr("sys.stdin", None)
    assert tui.main(["-C", str(proj), "--mode", "yolo", "first question"]) == 0
    assert tui.main(["sessions", "--json"]) == 0
    rows = json.loads(_json_tail(capsys, tui, ["sessions", "--json"]))
    mine = [r for r in rows if r["cwd"] == str(proj.resolve())]
    assert mine and mine[0]["title"] == "first question" and mine[0]["source"] == "print"
    sid = mine[0]["id"]

    assert tui.main(["logs", sid[:6]]) == 0
    out = capsys.readouterr().out
    assert "first question" in out and "List" in out and "Write" in out
    assert tui.main(["logs", "no-such-session"]) == 1

    assert tui.main(["-C", str(proj), "-c", "second question"]) == 0     # continue the last one in this folder
    assert tui.main(["-r", sid[:6], "third question"]) == 0              # resume by id, from anywhere
    capsys.readouterr()
    from vesper.store import Store as S

    assert sum(m.type == "human" for m in S().load_messages(sid)) == 3
    assert tui.main(["-r", "no-such-session", "x"]) == 2


def _json_tail(capsys, tui, argv):
    capsys.readouterr()
    tui.main(argv)
    return capsys.readouterr().out


# ---------------------------------------------------------------- desktop engine
def test_engine_folder_follows_the_agent_and_saves_history(server, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    engine = server.Engine()
    engine.update({"workspace": str(proj), "agent_mode": "yolo"})
    llm = fake(call("change_directory", path="sub"), AIMessage("Moved."), AIMessage("Still here."))
    monkeypatch.setattr(engine, "llm", lambda: llm)
    events = list(engine.stream_answer("go into sub", "chat-a", []))
    sub = str((proj / "sub").resolve())
    assert ("cwd", {"path": sub}) in events and events[-1][0] == "done"
    assert engine.settings["workspace"] == str(proj)     # other chats keep their chosen default
    list(engine.stream_answer("where are we?", "chat-a", []))
    session = engine._sessions["chat-a"]["session"]
    assert str(session.workspace.root) == sub and len(session.history) == 6
    rows = engine.history_db().list_sessions()
    assert rows and rows[0]["source"] == "desktop" and rows[0]["cwd"] == sub
    engine.update({"workspace": str(tmp_path)})           # another chat picks a different folder
    engine._sessions["chat-a"]["session"].set_llm(fake(AIMessage("Still here.")))
    list(engine.stream_answer("and now?", "chat-a", []))
    assert session.workspace.root == (proj / "sub").resolve()


# ---------------------------------------------------------------- reasoning and the transcript
def test_think_tags_are_split_from_the_answer_even_across_pieces():
    from vesper.coder import ThinkSplitter, split_thinking

    s = ThinkSplitter()
    got = []
    for piece in ["<thi", "nk>plan the", " work</th", "ink>\nThe answer", " is 4 < 5."]:
        got += s.feed(piece)
    got += s.flush()
    assert "".join(t for k, t in got if k == "reasoning") == "plan the work"
    assert "".join(t for k, t in got if k == "token") == "\nThe answer is 4 < 5."
    assert split_thinking("no tags here") == ("", "no tags here")
    assert split_thinking("<think>unfinished") == ("unfinished", "")


def test_reasoning_events_and_transcript(store, proj):
    from vesper.coder import reasoning_of, transcript

    thinking = AIMessage(content=[{"type": "thinking", "thinking": "Look at the file first."},
                                  {"type": "text", "text": ""}],
                         tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "c1"}])
    assert reasoning_of(thinking) == "Look at the file first."
    assert reasoning_of(AIMessage(content="x", additional_kwargs={"reasoning_content": "hmm"})) == "hmm"
    s = Session(fake(thinking, AIMessage("<think>It says alpha.</think>The file says alpha.")), Workspace(proj), store)
    events = list(s.ask("what is in a.txt?"))
    assert [d["text"] for e, d in events if e == "reasoning"] == ["Look at the file first.", "It says alpha."]
    assert "".join(d["text"] for e, d in events if e == "token") == "The file says alpha."

    turns = transcript(store.load_messages(s.id))
    assert turns[0].pop("turn").startswith("vesper-turn-")
    assert turns[0] == {"role": "user", "text": "what is in a.txt?"}
    kinds = [p["type"] for p in turns[1]["parts"]]
    assert kinds == ["reasoning", "tool", "reasoning", "text"]
    tool = turns[1]["parts"][1]
    assert tool["name"] == "read_file" and tool["ok"] is True and "alpha" in tool["output"]
    assert turns[1]["parts"][3]["text"] == "The file says alpha."


def test_engine_session_history_api(server, make_client, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("alpha\n")
    engine = server.Engine()
    engine.update({"workspace": str(proj), "agent_mode": "yolo"})
    llm = fake(call("run_command", command="echo hello"), AIMessage("<think>done</think>It printed hello."),
               AIMessage("Second answer."))
    monkeypatch.setattr(engine, "llm", lambda: llm)
    events = list(engine.stream_answer("say hello", "chat-1", []))
    kinds = [e for e, _ in events]
    assert kinds[0] == "session" and "output" in kinds and "reasoning" in kinds and "usage" in kinds
    sid = events[0][1]["id"]
    assert ("output", {"line": "hello"}) in events

    client = make_client(engine=engine)
    rows = client.get("/api/sessions").json()["sessions"]
    assert rows[0]["id"] == sid and rows[0]["title"] == "say hello" and rows[0]["source"] == "desktop"
    saved = client.get(f"/api/sessions/{sid}").json()
    assert saved["turns"][0].pop("turn").startswith("vesper-turn-")
    assert saved["turns"][0] == {"role": "user", "text": "say hello"}
    parts = saved["turns"][1]["parts"]
    assert [p["type"] for p in parts] == ["tool", "reasoning", "text"] and "hello" in parts[0]["output"]
    assert client.get("/api/sessions/ffffffff").status_code == 404
    assert client.get("/api/sessions/..%2F..").status_code in (400, 404)

    # Another chat (or the app after a restart) continues the saved conversation, in its folder.
    engine.forget_chat("chat-1")
    engine.update({"workspace": str(tmp_path)})
    events = list(engine.stream_answer("and again?", "chat-2", [], sid))
    assert events[0] == ("session", {"id": sid, "cwd": str(proj.resolve())})
    assert engine.settings["workspace"] == str(tmp_path)
    assert len(client.get(f"/api/sessions/{sid}").json()["turns"]) == 4
    assert client.post("/api/chat", json={"question": "x", "session_id": "../etc"}).status_code == 400

    assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
    assert client.get(f"/api/sessions/{sid}").status_code == 404 and "chat-2" not in engine._sessions


def test_one_shot_does_not_wait_forever_on_a_silent_stdin(proj):
    """A script or CI step can start vesper with a stdin that is open but never written to."""
    import os
    import subprocess
    import sys

    if os.name != "posix":
        pytest.skip("select() on pipes is POSIX only")
    read_end, write_end = os.pipe()  # the write end stays open: no data, no EOF
    try:
        done = subprocess.run(
            [sys.executable, "-m", "vesper.tui", "-C", str(proj), "--mode", "ask", "hello there"],
            stdin=read_end, capture_output=True, text=True, timeout=60,
            env={**os.environ, "VESPER_FAKE_LLM": "1"},
        )
    finally:
        os.close(read_end)
        os.close(write_end)
    assert done.returncode == 0 and "Let me look at the folder first." in done.stdout


# ---------------------------------------------------------------- terminal: deleting, renaming, attaching
def test_vesper_sessions_rm_and_rename(store, proj, capsys, monkeypatch):
    from vesper import tui

    monkeypatch.setattr(tui, "open_store", lambda: store)

    a = Session(fake(AIMessage("one")), Workspace(proj), store)
    list(a.ask("first question"))
    b = Session(fake(AIMessage("two")), Workspace(proj), store)
    list(b.ask("second question"))
    assert tui.main(["sessions", "rename", a.id, "Better", "name"]) == 0
    assert store.get_session(a.id)["title"] == "Better name"
    assert tui.main(["sessions", "rm", a.id, "nope123"]) == 1        # one unknown id: nothing deleted
    assert store.get_session(a.id) is not None
    assert tui.main(["sessions", "rm", a.id, b.id[:6]]) == 0
    assert store.get_session(a.id) is None and store.get_session(b.id) is None
    assert tui.main(["sessions", "rm"]) == 2
    assert tui.main(["session"]) == 2 and "Did you mean `vesper sessions`" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        tui.main(["logs", "-n", "-3"])


def test_mentions_attach_files_folders_and_images(proj):
    from vesper import tui
    from vesper.attachments import IMAGE_TYPES

    (proj / "docs dir").mkdir()
    (proj / "docs dir" / "a.txt").write_text("alpha\n")
    (proj / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    app = tui.App.__new__(tui.App)
    app.workspace = Workspace(proj)
    text, images = app.expand_mentions('look at @"docs dir" and @pic.png and @missing.txt')
    assert '<folder path="docs dir">' in text and "a.txt" in text
    assert len(images) == 1 and images[0]["mime_type"] == IMAGE_TYPES[".png"]
    dropped, images = app.expand_mentions(f"what is {proj / 'pic.png'}")
    assert len(images) == 1 and dropped.startswith("what is")
    from vesper.tui import DROPPED  # what a Windows terminal pastes
    assert [m.group(3) for m in DROPPED.finditer(r"see C:\Users\me\pic.png")] == [r"C:\Users\me\pic.png"]
    assert [m.group(2) for m in DROPPED.finditer(r'see "C:\Users\me\my pic.png"')] == [r"C:\Users\me\my pic.png"]


# ---------------------------------------------------------------- nothing is lost when a turn is cut short
def test_question_and_finished_steps_are_saved_while_the_turn_runs(proj, store):
    s = Session(fake(call("list_files", path="."), AIMessage("done")), Workspace(proj), store)
    seen = []
    for event, _ in s.ask("look around"):
        if event == "tool_end":  # mid-turn: what a force quit right now would leave on disk
            seen = store.load_messages(s.id)
    assert [m.type for m in seen][:3] == ["human", "ai", "tool"] and seen[0].content == "look around"
    assert [m.type for m in store.load_messages(s.id)] == ["human", "ai", "tool", "ai"]


def test_a_stopped_answer_keeps_what_was_written(proj, store):
    llm = ToolFake(messages=iter([AIMessage("one two three four five six seven")]))  # streams word by word
    s = Session(llm, Workspace(proj), store)
    turn = s.ask("count")
    for event, data in turn:
        if event == "token" and "three" in data["text"]:
            break
    turn.close()  # what Stop and closing the window do
    saved = store.load_messages(s.id)
    assert saved[0].content == "count"
    assert saved[-1].type == "ai" and saved[-1].content.startswith("one two three") and "[stopped]" in saved[-1].content


def test_a_failed_turn_keeps_the_question(proj, store):
    class Down(ToolFake):
        def _generate(self, *a, **k):
            raise RuntimeError("provider is down")

    s = Session(Down(disable_streaming=True, messages=iter([])), Workspace(proj), store)
    with pytest.raises(RuntimeError):
        list(s.ask("are you there?"))
    assert [m.content for m in store.load_messages(s.id)] == ["are you there?"]


# ---------------------------------------------------------------- rewind
def test_rewind_forgets_a_message_and_what_followed_and_can_put_files_back(proj, store):
    ws = Workspace(proj, mode="yolo")
    s = Session(fake(
        call("write_file", path="a.txt", content="first edit\n"), AIMessage("did one"),
        call("write_file", path="new.txt", content="made\n"), AIMessage("did two"),
        AIMessage("three"),
    ), ws, store)
    list(s.ask("change a"))
    list(s.ask("add a file"))
    list(s.ask("just talk"))
    assert (proj / "a.txt").read_text() == "first edit\n" and (proj / "new.txt").exists()
    with pytest.raises(ValueError, match="changed"):
        s.rewind(1, expect="something else")
    out = s.rewind(1, expect="add a", files=True)  # the second-to-last message
    assert out["text"] == "add a file" and out["files_known"]
    assert [Path(p).name for p in out["restored"]] == ["new.txt"]
    assert not (proj / "new.txt").exists() and (proj / "a.txt").read_text() == "first edit\n"
    assert [m.content for m in store.load_messages(s.id) if m.type == "human"] == ["change a"]
    out = s.rewind(0, files=False)  # conversation only: the file stays as the agent left it
    assert out["text"] == "change a" and out["restored"] == [] and s.history == []
    assert (proj / "a.txt").read_text() == "first edit\n"


def test_rewind_endpoint_validates_and_refuses_a_busy_chat(proj):
    from fastapi.testclient import TestClient

    from vesper import server

    app = server.create_app("t", engine=server.Engine())
    client = TestClient(app, headers={"X-Token": "t", "Host": "127.0.0.1"})
    assert client.post("/api/chats/../rewind", json={"from_end": 0}).status_code in (400, 404)
    assert client.post("/api/chats/c1/rewind", json={"from_end": -1}).status_code == 400
    assert client.post("/api/chats/c1/rewind", json={"from_end": True}).status_code == 400
    assert client.post("/api/chats/c1/rewind", json={"from_end": 0, "turn": "x; drop"}).status_code == 400


def test_terminal_rewind_puts_the_message_back_in_the_prompt(proj, store, monkeypatch):
    from vesper import tui

    s = Session(fake(AIMessage("one"), AIMessage("two")), Workspace(proj), store)
    list(s.ask("first thing"))
    list(s.ask("second thing"))
    app = tui.App.__new__(tui.App)
    app.session, app.workspace, app._prefill = s, s.workspace, ""
    app.wait_idle = lambda: None
    answers = iter(["", "n"])  # the default (1 = latest), no files
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    app.rewind("")
    assert app._prefill == "second thing"
    assert [m.content for m in store.load_messages(s.id) if m.type == "human"] == ["first thing"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    app.rewind("9")  # out of range: nothing happens
    assert [m.content for m in s.history if m.type == "human"] == ["first thing"]


def test_rewind_by_turn_id_names_the_exact_message(proj, store):
    s = Session(fake(AIMessage("one"), AIMessage("two"), AIMessage("three")), Workspace(proj), store)
    events = list(s.ask("first"))
    first_id = next(d["id"] for e, d in events if e == "turn")
    list(s.ask("second"))
    list(s.ask("first"))  # same words again: only the id tells them apart
    ids = [t["turn"] for t in transcript(s.history) if t["role"] == "user"]
    assert ids[0] == first_id and len(set(ids)) == 3
    with pytest.raises(ValueError):
        s.rewind(0, turn="vesper-turn-1")
    out = s.rewind(0, turn=ids[1])  # from_end is ignored when the id is known
    assert out["text"] == "second"
    assert [m.content for m in s.history if m.type == "human"] == ["first"]


def test_an_answer_cut_off_by_an_error_says_so(proj, store):
    class Breaks(ToolFake):
        def _stream(self, *a, **k):
            yield from super()._stream(*a, **k)
            raise RuntimeError("connection reset")

    s = Session(Breaks(messages=iter([AIMessage("half an answer")])), Workspace(proj), store)
    with pytest.raises(RuntimeError):
        list(s.ask("go"))
    last = store.load_messages(s.id)[-1]
    assert last.type == "ai" and "half an answer" in last.content and "[cut off by an error]" in last.content
