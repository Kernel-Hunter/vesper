"""Hooks (hooks.py): the user's commands around the agent's steps. Offline."""
import json
import sys
import time
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import hooks
from vesper.coder import Session, user_text
from vesper.workspace import Workspace

SCRIPT = Path(__file__).with_name("hook_script.py")


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def fake(*replies):
    return ToolFake(disable_streaming=True, messages=iter(replies))


def cmd(mode: str, log: Path) -> str:
    return f'"{sys.executable}" "{SCRIPT}" {mode} "{log}"'


def config(event: str, command: str, matcher: str = "") -> str:
    return json.dumps({"hooks": {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]}})


def tool(ws: Workspace, name: str):
    return next(t for t in ws.tools() if t.name == name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A project folder, a log file, and a user hooks file of its own."""
    proj = tmp_path / "proj"
    proj.mkdir()
    user = tmp_path / "user-hooks.json"
    monkeypatch.setattr(hooks, "user_file", lambda: user)
    notices: list[str] = []
    ws = Workspace(proj, mode="yolo", hooks=hooks.Runner(notice=notices.append))
    return ws, user, tmp_path / "log.jsonl", notices


def logged(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_pre_tool_hook_blocks_the_step(env):
    ws, user, log, _ = env
    user.write_text(config("PreToolUse", cmd("block", log), "Write|edit_file"))  # Claude Code's name matches too
    out = tool(ws, "write_file").invoke({"path": "a.txt", "content": "x"})
    assert out == "Error: a hook blocked this step: no writes here"
    assert not (ws.root / "a.txt").exists()
    seen = logged(log)[0]
    assert seen["hook_event_name"] == "PreToolUse" and seen["tool_name"] == "write_file"
    assert seen["tool_input"] == {"path": "a.txt", "content": "x"}
    (ws.root / "b.txt").write_text("hi\n")
    assert "hi" in tool(ws, "read_file").invoke({"path": "b.txt"})  # the matcher leaves other tools alone


def test_a_post_tool_hook_talks_back_to_the_agent(env):
    ws, user, log, _ = env
    user.write_text(config("PostToolUse", cmd("block", log)))
    out = tool(ws, "write_file").invoke({"path": "a.txt", "content": "x"})
    assert out.startswith("Created a.txt") and out.endswith("A hook says: no writes here")
    assert logged(log)[0]["tool_response"].startswith("Created a.txt")


def test_a_failing_or_slow_hook_is_reported_and_the_step_goes_on(env):
    ws, user, log, notices = env
    user.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": cmd("fail", log)},
        {"type": "command", "command": cmd("sleep", log), "timeout": 1}]}]}}))
    started = time.monotonic()
    assert tool(ws, "write_file").invoke({"path": "a.txt", "content": "x"}).startswith("Created")
    assert time.monotonic() - started < 10
    assert any("exit code 1: something broke" in n for n in notices)
    assert any("did not finish within 1 s" in n for n in notices)


def test_project_hooks_wait_for_approval(env):
    ws, _, log, notices = env
    (ws.root / ".vesper").mkdir()
    project = ws.root / ".vesper" / "hooks.json"
    project.write_text(config("PreToolUse", cmd("block", log)))
    assert tool(ws, "write_file").invoke({"path": "a.txt", "content": "x"}).startswith("Created")
    assert any("not approved yet" in n for n in notices) and not log.exists()
    assert hooks.approve(ws.root) == project
    assert "blocked" in tool(ws, "write_file").invoke({"path": "b.txt", "content": "x"})
    time.sleep(0.05)
    project.write_text(config("PreToolUse", cmd("block", log), "edit_file"))  # changed: needs a new approval
    assert hooks.configured(ws.root)[1] == project


def test_the_agent_must_ask_to_write_a_hooks_file(env):
    ws, _, _, _ = env
    asked = []
    ws.approve = lambda action: asked.append(action) or "no"
    out = tool(ws, "write_file").invoke({"path": ".vesper/hooks.json", "content": "{}"})
    assert "declined" in out and asked and asked[0]["strict"]  # even in yolo mode


def test_a_prompt_hook_blocks_or_adds_context(env):
    ws, user, log, _ = env
    user.write_text(config("UserPromptSubmit", cmd("context", log)))
    s = Session(fake(AIMessage("ok")), ws)
    list(s.ask("ship it"))
    sent = s.history[0]
    assert "Today is release day" in sent.content and user_text(sent) == "ship it"
    assert logged(log)[0]["prompt"] == "ship it"
    user.write_text(config("UserPromptSubmit", cmd("block", log)))
    s2 = Session(fake(), ws)  # no replies: the model must not be called
    events = list(s2.ask("delete everything"))
    assert ("notice", {"text": "A hook blocked this message: no writes here"}) in events and s2.history == []


def test_a_stop_hook_sends_the_agent_back_once(env):
    ws, user, log, _ = env
    user.write_text(config("Stop", cmd("once", log)))
    s = Session(fake(AIMessage("Done."), AIMessage("Tests run. Done.")), ws)
    events = list(s.ask("fix it"))
    texts = "".join(d.get("text", "") for e, d in events if e == "token")
    assert "Tests run. Done." in texts and len(logged(log)) == 2
    assert any(e == "notice" and "Stop hook" in d["text"] for e, d in events)


def test_matchers():
    assert hooks.matches("", "grep") and hooks.matches("*", "grep")
    assert hooks.matches("Bash", "run_command") and hooks.matches("run_.*", "run_command")
    assert not hooks.matches("Read", "write_file") and not hooks.matches("[", "grep")
