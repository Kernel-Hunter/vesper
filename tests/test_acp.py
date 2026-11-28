"""ACP (acp.py): Vesper as an agent for editors. Offline, with a fake editor."""
import base64
import io
import json
import threading

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import acp
from vesper.store import Store


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class Editor(acp.Connection):
    """Records what Vesper sends and answers its permission requests with `choice`."""

    def __init__(self, choice="yes"):
        super().__init__(io.BytesIO(), io.BytesIO())
        self.sent: list[dict] = []
        self.choice = choice
        self.asked: list[dict] = []

    def send(self, msg):
        msg = json.loads(json.dumps(msg, default=str))
        self.sent.append(msg)
        if msg.get("method") == "session/request_permission":
            self.asked.append(msg["params"])
            if self.choice is not None:
                outcome = ({"outcome": "cancelled"} if self.choice == "cancelled"
                           else {"outcome": "selected", "optionId": self.choice})
                threading.Thread(target=self.answered, args=({"id": msg["id"], "result": {"outcome": outcome}},)).start()

    def updates(self, kind=None):
        return [m["params"]["update"] for m in self.sent if m.get("method") == "session/update"
                and (kind is None or m["params"]["update"]["sessionUpdate"] == kind)]

    def reply(self, rid):
        return next(m for m in self.sent if m.get("id") == rid and "method" not in m)


def agent_with(editor, *replies, mode="ask"):
    model = ToolFake(disable_streaming=True, messages=iter(replies))
    return acp.Agent(editor, store=Store(), cfg={"provider": "fake", "model": "m", "agent_mode": mode},
                     make_llm=lambda cfg: model)


def call(agent, rid, method, params):
    thread = agent.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    if thread is not None:
        thread.join(30)
    return agent.conn.reply(rid)


def write_call(path="notes.md", content="hello\n"):
    return AIMessage("", tool_calls=[{"name": "write_file", "args": {"path": path, "content": content}, "id": "w1"}])


def test_a_prompt_streams_steps_and_asks_before_writing(tmp_path):
    editor = Editor("yes")
    agent = agent_with(editor, write_call(), AIMessage("Wrote it."))
    init = call(agent, 1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]
    assert init["protocolVersion"] == 1 and init["agentCapabilities"]["loadSession"] is True
    new = call(agent, 2, "session/new", {"cwd": str(tmp_path), "mcpServers": []})["result"]
    assert new["modes"]["currentModeId"] == "ask"
    sid = new["sessionId"]
    done = call(agent, 3, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "write notes"}]})
    assert done["result"] == {"stopReason": "end_turn"}
    assert (tmp_path / "notes.md").read_text() == "hello\n"
    start = editor.updates("tool_call")[0]
    assert start["toolCallId"] == "w1" and start["kind"] == "edit" and start["status"] == "in_progress"
    assert start["content"][0] == {"type": "diff", "path": str(tmp_path / "notes.md"), "oldText": None, "newText": "hello\n"}
    assert start["locations"] == [{"path": str(tmp_path / "notes.md")}]
    asked = editor.asked[0]
    assert asked["sessionId"] == sid and asked["toolCall"]["toolCallId"] == "w1"
    assert [o["kind"] for o in asked["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert editor.updates("tool_call_update")[0]["status"] == "completed"
    assert "".join(u["content"]["text"] for u in editor.updates("agent_message_chunk")) == "Wrote it."


@pytest.mark.parametrize("choice", ["no", "cancelled"])
def test_a_rejected_or_dismissed_permission_writes_nothing(tmp_path, choice):
    editor = Editor(choice)
    agent = agent_with(editor, write_call(), AIMessage("Okay, I left it."))
    sid = call(agent, 1, "session/new", {"cwd": str(tmp_path), "mcpServers": []})["result"]["sessionId"]
    call(agent, 2, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "write"}]})
    assert not (tmp_path / "notes.md").exists()
    assert editor.updates("tool_call_update")[0]["status"] == "failed"


def test_modes_and_errors(tmp_path):
    editor = Editor(None)  # never answers: yolo mode must not ask
    agent = agent_with(editor, write_call(), AIMessage("Done."))
    sid = call(agent, 1, "session/new", {"cwd": str(tmp_path), "mcpServers": []})["result"]["sessionId"]
    assert call(agent, 2, "session/set_mode", {"sessionId": sid, "modeId": "nope"})["error"]["code"] == -32602
    assert call(agent, 3, "session/set_mode", {"sessionId": sid, "modeId": "yolo"})["result"] == {}
    call(agent, 4, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "write"}]})
    assert (tmp_path / "notes.md").exists() and editor.asked == []
    assert call(agent, 5, "session/prompt", {"sessionId": "missing", "prompt": []})["error"]["code"] == -32002
    assert call(agent, 6, "session/fork", {})["error"]["code"] == -32601
    assert "error" in call(agent, 7, "session/new", {"cwd": str(tmp_path / "missing"), "mcpServers": []})


def test_cancel_ends_the_turn_as_cancelled(tmp_path):
    editor = Editor(None)  # the permission stays open until the cancel
    agent = agent_with(editor, write_call(), AIMessage("never"))
    sid = call(agent, 1, "session/new", {"cwd": str(tmp_path), "mcpServers": []})["result"]["sessionId"]
    thread = agent.handle({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                           "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "write"}]}})
    for _ in range(200):
        if editor.asked:
            break
        threading.Event().wait(0.05)
    agent.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    thread.join(30)
    assert editor.reply(2)["result"] == {"stopReason": "cancelled"}
    assert not (tmp_path / "notes.md").exists()


def test_a_saved_session_loads_and_replays(tmp_path):
    editor = Editor("yes")
    agent = agent_with(editor, AIMessage("First answer."))
    sid = call(agent, 1, "session/new", {"cwd": str(tmp_path), "mcpServers": []})["result"]["sessionId"]
    call(agent, 2, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi there"}]})
    later = Editor()
    other = agent_with(later)
    assert call(other, 1, "session/load", {"sessionId": sid, "cwd": str(tmp_path), "mcpServers": []})["result"]["modes"]
    kinds = [(u["sessionUpdate"], u["content"]["text"]) for u in later.updates() if "content" in u]
    assert kinds == [("user_message_chunk", "hi there"), ("agent_message_chunk", "First answer.")]
    assert call(other, 2, "session/load", {"sessionId": "nope", "cwd": str(tmp_path), "mcpServers": []})["error"]


def test_prompt_blocks_become_the_message():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    text, images = acp.prompt_parts([
        {"type": "text", "text": "Look at this"},
        {"type": "resource", "resource": {"uri": "file:///p/a.py", "text": "x = 1"}},
        {"type": "resource_link", "uri": "file:///p/b.py", "name": "b.py"},
        {"type": "image", "mimeType": "image/png", "data": png},
        {"type": "image", "mimeType": "image/png", "data": "not base64!"},
        {"type": "audio", "mimeType": "audio/wav", "data": ""},
    ])
    assert text == 'Look at this\n\n<file path="/p/a.py">\nx = 1\n</file>\n\n(file: /p/b.py)'
    assert len(images) == 1


def test_serve_reads_lines_and_answers_bad_json():
    inp = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n\nnot json\n')
    out = io.BytesIO()
    acp.Agent(acp.Connection(inp, out), store=None, cfg={}).serve()
    lines = [json.loads(line) for line in out.getvalue().decode().splitlines()]
    assert lines[0]["result"]["agentCapabilities"]["loadSession"] is False
    assert lines[1]["error"]["code"] == -32700
