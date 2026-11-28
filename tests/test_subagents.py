"""Helpers (subagents.py): child agents with their own context. Offline, scripted model."""
import threading
import time

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from vesper import subagents
from vesper.coder import Session
from vesper.workspace import Workspace

_lock = threading.Lock()
_seen = {"busy": 0, "most": 0}


class Scripted(BaseChatModel):
    """The agent asks two helpers at once; each helper greps once and answers."""

    always_search: bool = False
    helper_writes: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        task = next((m.content for m in messages if m.type == "human"), "")
        if "You are a helper" in system:
            with _lock:
                _seen["busy"] += 1
                _seen["most"] = max(_seen["most"], _seen["busy"])
            time.sleep(0.3)  # long enough for the other helper to start
            with _lock:
                _seen["busy"] -= 1
            if self.helper_writes and not tool_results:
                msg = AIMessage("", tool_calls=[{"name": "write_file", "args": {"path": "x", "content": "x"}, "id": "w"}])
            elif self.always_search or not tool_results:
                word = "alpha" if "alpha" in task else "beta"
                msg = AIMessage("", tool_calls=[{"name": "grep", "args": {"pattern": word}, "id": f"g{len(tool_results)}"}])
            else:
                msg = AIMessage(f"Found: {tool_results[-1].content.strip()}")
        elif not tool_results:
            msg = AIMessage("", tool_calls=[
                {"name": "subagent", "args": {"task": "Where is alpha defined?"}, "id": "s1"},
                {"name": "subagent", "args": {"task": "Where is beta defined?"}, "id": "s2"}])
        else:
            msg = AIMessage("Both found.")
        msg.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _session(tmp_path, **model):
    _seen.update(busy=0, most=0)
    (tmp_path / "a.py").write_text("alpha = 1\n")
    (tmp_path / "b.py").write_text("beta = 2\n")
    lines = []
    ws = Workspace(tmp_path, mode="yolo", on_output=lines.append)
    return Session(Scripted(**model), ws), lines


def test_two_helpers_run_at_once_and_only_their_findings_come_back(tmp_path):
    s, lines = _session(tmp_path)
    events = list(s.ask("find alpha and beta"))
    ends = {d["id"]: d for e, d in events if e == "tool_end"}
    assert "Found: a.py:1: alpha = 1" in ends["s1"]["output"] and "(helper" in ends["s1"]["output"]
    assert "Found: b.py:1: beta = 2" in ends["s2"]["output"]
    assert _seen["most"] == 2  # in parallel, not one after the other
    assert any(line.startswith("helper ") and "grep alpha" in line for line in lines)
    usage = next(d for e, d in events if e == "usage")
    assert usage["input"] == 6 * 100  # 2 agent calls + 2 helpers x 2 calls


def test_a_helper_cannot_change_anything(tmp_path):
    s, _ = _session(tmp_path, helper_writes=True)
    child = subagents._helper_workspace(s.workspace)
    names = {t.name for t in child.tools() if t.name in subagents.TOOLS}
    assert names == set(subagents.TOOLS) and "write_file" not in names
    list(s.ask("find alpha and beta"))
    assert not (tmp_path / "x").exists()


def test_a_helper_that_never_finishes_is_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(subagents, "MAX_STEPS", 2)
    s, _ = _session(tmp_path, always_search=True)
    out = subagents.run(s, "Where is alpha defined?", 7)
    assert "ran out of steps" in out


def test_stopping_the_turn_stops_helpers(tmp_path):
    s, _ = _session(tmp_path)
    s.workspace.stopped = True
    assert subagents.run(s, "Where is alpha defined?", 1).startswith("Error: the user stopped")
    assert subagents.run(s, "  ", 2).startswith("Error: the task is empty")


def test_the_agent_is_told_how_to_use_helpers(tmp_path):
    from vesper.prompts import agent_system_prompt

    assert "subagent" in agent_system_prompt(tmp_path)
    tool = subagents.make_tool(None)
    assert tool.name == "subagent" and len(tool.description) < 200


def test_a_helper_never_waits_for_an_approval(tmp_path):
    s, _ = _session(tmp_path)
    s.workspace.mode = "ask"
    asked = []
    s.workspace.approve = lambda action: asked.append(action) or "yes"
    child = subagents._helper_workspace(s.workspace)
    web = next(t for t in child.tools() if t.name == "web_fetch")
    out = web.invoke({"url": "https://example.com/page"})
    assert out.startswith("Error:") and "put the address in your answer" in out and asked == []
