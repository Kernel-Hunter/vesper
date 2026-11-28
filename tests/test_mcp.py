"""MCP client: real servers (a stdio child process and an HTTP server), approval, the agent."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import mcp
from vesper.coder import Session
from vesper.workspace import Workspace

SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        self.__dict__["bound"] = [getattr(t, "name", t) for t in tools]
        return self


def call(name, call_id="c1", **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture(autouse=True)
def fresh_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("VESPER_HOME", str(tmp_path / "vhome"))
    mcp._manager = None
    yield
    if mcp._manager is not None:
        mcp._manager.close()
    mcp._manager = None


def write_config(path: Path, servers: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def fake_spec(extra=0, **more):
    return {"command": sys.executable, "args": ["-I", SERVER, str(extra)], **more}


def test_stdio_server_lists_every_page_answers_requests_and_calls_tools(tmp_path):
    write_config(tmp_path / "vhome" / "mcp.json", {"fake": fake_spec()})
    servers = mcp.manager().for_root(str(tmp_path))
    mcp.Manager.wait(servers, 15)
    s = servers[0]
    assert s.status == "ready", s.error
    assert [t["name"] for t in s.tools] == ["echo", "add_note", "fail"]  # both pages, despite stdout noise
    assert s.call("echo", {"text": "hi"}) == "echo: hi"
    out = s.call("add_note", {"note": "x"})
    assert out.startswith("saved 1 note(s)") and "[image image/png, 6 bytes]" in out
    assert s.call("fail", {}) == "Error: it broke"
    assert mcp.status(tmp_path)[0] == {"name": "fake", "scope": "user", "file": str(tmp_path / "vhome" / "mcp.json"),
                                       "status": "ready", "error": None, "tools": ["echo", "add_note", "fail"],
                                       "transport": "stdio"}
    proc = s.conn.proc
    mcp.manager().close()
    assert proc.poll() is not None  # no server is left running


def test_broken_servers_fail_alone_with_a_reason(tmp_path):
    write_config(tmp_path / "vhome" / "mcp.json", {
        "missing": {"command": "/no/such/program-xyz"},
        "silent": {"command": sys.executable, "args": ["-c", "import time; time.sleep(30)"], "startup_timeout": 0.5},
        "good": fake_spec(),
        "bad name!": {"command": "x"},
        "nothing": {"args": []},
    })
    servers = mcp.manager().for_root(str(tmp_path))
    mcp.Manager.wait(servers, 15)
    by = {s.name: s for s in servers}
    assert set(by) == {"missing", "silent", "good"}
    assert by["missing"].status == "failed" and "could not start" in by["missing"].error
    assert by["silent"].status == "failed" and "no answer to initialize" in by["silent"].error
    assert by["good"].status == "ready"
    assert "skipped bad name!, nothing" in mcp.problems(tmp_path)[0]


def test_project_servers_need_approval_and_a_changed_definition_asks_again(tmp_path):
    proj = tmp_path / "proj"
    cfg = write_config(proj / ".mcp.json", {"fake": fake_spec()})
    (proj / "sub").mkdir()
    assert mcp.find_project_config(proj / "sub") == cfg  # found from a sub folder too
    assert mcp.manager().for_root(str(proj)) == []  # not approved: not started
    assert [s["status"] for s in mcp.status(proj)] == ["needs approval"]
    assert mcp.approve(proj) == ["fake"]
    assert mcp.configured(proj)[0]["trusted"]
    write_config(proj / ".mcp.json", {"fake": fake_spec(extra=1)})  # someone changed the command
    assert not mcp.configured(proj)[0]["trusted"]


def test_env_values_are_filled_from_the_environment(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc")
    assert mcp._expand({"env": {"T": "${MY_TOKEN}", "U": "${NOT_SET_ANYWHERE}"}, "args": ["--t=${MY_TOKEN}"]}) == \
        {"env": {"T": "abc", "U": ""}, "args": ["--t=abc"]}


def _agent_session(tmp_path, replies, mode="ask", extra=0, answers=None):
    write_config(tmp_path / "vhome" / "mcp.json", {"fake": fake_spec(extra)})
    proj = tmp_path / "work"
    proj.mkdir(exist_ok=True)
    asked = []

    def approve(action):
        asked.append(action)
        return (answers or ["yes"] * 9)[len(asked) - 1]

    ws = Workspace(proj, mode=mode, approve=approve)
    llm = ToolFake(disable_streaming=True, messages=iter(replies))
    return Session(llm, ws), llm, asked


def test_the_agent_uses_mcp_tools_and_asks_before_ones_that_change_things(tmp_path):
    s, llm, asked = _agent_session(tmp_path, [
        call("mcp__fake__echo", text="read only"),
        call("mcp__fake__add_note", call_id="c2", note="remember"),
        AIMessage("done"),
    ])
    events = list(s.ask("use the tools"))
    assert {"mcp__fake__echo", "mcp__fake__add_note", "mcp__fake__fail"} <= set(llm.bound)
    outputs = [d["output"] for e, d in events if e == "tool_end"]
    assert outputs[0] == "echo: read only" and outputs[1].startswith("saved 1 note(s)")
    assert len(asked) == 1  # the read-only tool did not ask
    a = asked[0]
    assert a["kind"] == "mcp" and a["server"] == "fake" and a["mcp_tool"] == "add_note" and '"note": "remember"' in a["args"]
    assert a["rule"] == "mcp__fake__add_note" and a["tool"] == "mcp__fake__add_note"  # the step the app shows it on


def test_always_saves_a_rule_for_that_one_tool_and_no_answer_refuses(tmp_path):
    s, llm, asked = _agent_session(tmp_path, [
        call("mcp__fake__add_note", note="a"), call("mcp__fake__add_note", call_id="c2", note="b"),
        call("mcp__fake__fail", call_id="c3"), AIMessage("ok"),
    ], answers=["always", "no"])
    events = list(s.ask("go"))
    outputs = [d["output"] for e, d in events if e == "tool_end"]
    assert outputs[1].startswith("saved 2 note(s)") and len(asked) == 2  # the second note did not ask
    assert "declined" in outputs[2]
    assert "mcp__fake__add_note" in s.workspace.rules  # the front end saves it through on_rule


def test_plan_mode_allows_only_read_only_mcp_tools(tmp_path):
    s, llm, asked = _agent_session(tmp_path, [
        call("mcp__fake__echo", text="x"), call("mcp__fake__add_note", call_id="c2", note="y"), AIMessage("plan"),
    ], mode="plan")
    outputs = [d["output"] for e, d in list(s.ask("plan it")) if e == "tool_end"]
    assert outputs[0] == "echo: x" and "Plan mode" in outputs[1] and asked == []


def test_many_tools_become_find_and_call(tmp_path):
    s, llm, asked = _agent_session(tmp_path, [
        call("mcp_find", query="note"),
        call("mcp_call", call_id="c2", server="fake", tool="add_note", arguments={"note": "z"}),
        call("mcp_call", call_id="c3", server="fake", tool="nope"),
        AIMessage("ok"),
    ], extra=mcp.DIRECT_TOOL_LIMIT)
    events = list(s.ask("go"))
    assert "mcp_find" in llm.bound and "mcp_call" in llm.bound and not any(n.startswith("mcp__") for n in llm.bound)
    outputs = [d["output"] for e, d in events if e == "tool_end"]
    assert "fake / add_note: Save a note." in outputs[0] and '"note"' in outputs[0]
    assert outputs[1].startswith("saved 1 note(s)")
    assert "has no tool 'nope'" in outputs[2]


def test_unapproved_project_servers_are_mentioned_once(tmp_path):
    proj = tmp_path / "proj"
    write_config(proj / ".mcp.json", {"fake": fake_spec()})
    s = Session(ToolFake(disable_streaming=True, messages=iter([AIMessage("a"), AIMessage("b")])), Workspace(proj))
    first = [d["text"] for e, d in s.ask("hi") if e == "notice"]
    second = [d["text"] for e, d in s.ask("again") if e == "notice"]
    assert any("not approved yet: fake" in t for t in first) and second == []


class _McpHttp(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _McpHttp.seen.append((msg.get("method"), self.headers.get("Mcp-Session-Id"), self.headers.get("Authorization")))
        if "id" not in msg:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if msg["method"] == "initialize":
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": mcp.PROTOCOL, "capabilities": {}}}
            stream = False
        elif msg["method"] == "tools/list":
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "time", "inputSchema": {"type": "object"}}]}}
            stream = True
        else:
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "noon"}]}}
            stream = True
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "sess-1")
        if stream:
            data = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: ' + data + b"\n\n"
            self.send_header("Content-Type", "text/event-stream")
        else:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        _McpHttp.seen.append(("DELETE", self.headers.get("Mcp-Session-Id"), None))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_http_server_with_json_and_event_stream_answers(tmp_path, monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _McpHttp)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("CLOCK_TOKEN", "s3cret")
    write_config(tmp_path / "vhome" / "mcp.json", {"clock": {
        "url": f"http://127.0.0.1:{httpd.server_port}/mcp", "headers": {"Authorization": "Bearer ${CLOCK_TOKEN}"}}})
    _McpHttp.seen = []
    try:
        servers = mcp.manager().for_root(str(tmp_path))
        mcp.Manager.wait(servers, 10)
        s = servers[0]
        assert s.status == "ready", s.error
        assert s.call("time", {}) == "noon"
        mcp.manager().close()
    finally:
        httpd.shutdown()
    methods = [m for m, _sid, _auth in _McpHttp.seen]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"]
    assert all(sid == "sess-1" for _m, sid, _a in _McpHttp.seen[1:])  # the session id goes back on every later call
    assert _McpHttp.seen[0][2] == "Bearer s3cret"


def test_result_text_is_capped():
    text = mcp.result_text({"content": [{"type": "text", "text": "x" * (mcp.MAX_RESULT_CHARS * 2)}]})
    assert len(text) < mcp.MAX_RESULT_CHARS + 100 and "characters cut" in text


def test_mcp_api_shows_saves_approves_and_validates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from vesper import server

    proj = tmp_path / "proj"
    write_config(proj / ".mcp.json", {"fake": fake_spec()})
    engine = server.Engine()
    engine.settings["workspace"] = str(proj)
    client = TestClient(server.create_app("t", engine=engine), headers={"X-Token": "t", "Host": "127.0.0.1"})
    state = client.get("/api/mcp").json()
    assert [(s["name"], s["status"], s["scope"]) for s in state["servers"]] == [("fake", "needs approval", "project")]
    assert client.post("/api/mcp/config", json={"text": "{not json"}).status_code == 400
    assert client.post("/api/mcp/config", json={"text": '{"mcpServers": []}'}).status_code == 400
    assert client.post("/api/mcp/approve", json={"name": "../x"}).status_code == 400
    assert client.post("/api/mcp/nope", json={}).status_code == 404
    client.post("/api/mcp/approve", json={"name": "fake"})
    mcp.Manager.wait(list(mcp.manager().servers.values()), 15)
    assert client.get("/api/mcp").json()["servers"][0]["status"] == "ready"
    saved = client.post("/api/mcp/config", json={"text": json.dumps({"mcpServers": {"mine": fake_spec()}})}).json()
    assert {s["name"] for s in saved["servers"]} == {"fake", "mine"} and '"mine"' in saved["config"]
    engine.shutdown(timeout=0.5)  # quitting stops every server
    assert mcp.manager().servers == {}


def test_servers_an_editor_hands_over_start_for_that_session(tmp_path):
    from vesper import acp

    specs = acp.mcp_specs([
        {"name": "fake", "command": sys.executable, "args": ["-I", SERVER, "0"], "env": [{"name": "X", "value": "1"}]},
        {"type": "http", "name": "web", "url": "https://example.com/mcp", "headers": [{"name": "Auth", "value": "t"}]},
        {"type": "sse", "name": "old", "url": "https://example.com/sse", "headers": []},
        {"name": "", "command": "x"},
    ])
    assert [s["name"] for s in specs] == ["fake", "web"]
    assert specs[0]["spec"]["env"] == {"X": "1"} and specs[1]["spec"] == {"url": "https://example.com/mcp", "headers": {"Auth": "t"}}
    proj = tmp_path / "work"
    proj.mkdir()
    llm = ToolFake(disable_streaming=True, messages=iter([call("mcp__fake__echo", text="hi"), AIMessage("done")]))
    s = Session(llm, Workspace(proj, mode="yolo"))
    s.extra_mcp = specs[:1]  # no mcp.json at all: the editor's server alone
    events = list(s.ask("echo"))
    assert "mcp__fake__echo" in llm.bound
    assert [d["output"] for e, d in events if e == "tool_end"] == ["echo: hi"]
