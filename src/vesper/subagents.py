"""Helpers: child agents with their own context, for searches and research.

The agent's `subagent` tool hands one self-contained question to a helper. The helper gets a
fresh conversation, read-only tools (list, read, find, search, and the web) and the same
model, works until it can answer, and only its final reply goes back to the agent. A broad
search through fifty files then costs the agent a few hundred words of findings instead of
the files themselves, and several helpers can run at the same time.

A helper never edits, runs commands or starts helpers of its own, and never asks the user
anything: a page fetch that would need approval is refused with a note to report the address,
so the agent can fetch it itself. Stopping the turn stops every helper at its next step. What
helpers spend is added to the turn's tokens and cost.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError

from vesper.workspace import ToolError, Workspace

if TYPE_CHECKING:
    from vesper.coder import Session

TOOLS = ("list_files", "read_file", "glob", "grep", "web_fetch", "web_search")
MAX_STEPS = 30          # model calls per helper
MAX_PARALLEL = 4        # helpers running at once; more wait for a free slot
MAX_REPLY = 8000        # characters of a helper's reply that go back to the agent
HISTORY_SHARE = 0.6     # share of the model's window a helper's own conversation may use

_slots = threading.BoundedSemaphore(MAX_PARALLEL)


def make_tool(session: Session):
    counter = {"n": 0}
    lock = threading.Lock()

    @tool
    def subagent(task: str) -> str:
        """Give a self-contained question to a helper with a fresh context that can read, search and browse but not change anything; it returns its findings."""
        with lock:
            counter["n"] += 1
            number = counter["n"]
        return run(session, task, number)

    return subagent


def _label(name: str, args: dict) -> str:
    key = {"list_files": "path", "read_file": "path", "glob": "pattern", "grep": "pattern",
           "web_fetch": "url", "web_search": "query"}.get(name, "")
    value = " ".join(str(args.get(key, "")).split())
    return f"{name} {value[:80]}".strip()


def _no_asking(action: dict) -> str:
    # The user sees the agent's steps, not a helper's, so a helper never waits for an answer.
    raise ToolError("This needs the user's approval, which a helper cannot ask for. Do not retry; "
                    "put the address in your answer so the agent can fetch it.")


def _helper_workspace(parent: Workspace) -> Workspace:
    return Workspace(parent.root, approve=_no_asking, mode=parent.mode, rules=list(parent.rules),
                     hosts=list(parent.hosts), always=set(parent.always), parent=parent, hooks=parent.hooks)


def build(session: Session, child: Workspace):
    from vesper import providers as prov
    from vesper.coder import CHARS_PER_TOKEN, fit_history, repair_tool_calls
    from vesper.prompts import HELPER_PROMPT, environment

    tools = [t for t in child.tools() if t.name in TOOLS]
    names = {t.name for t in tools}
    budget = int(session.context_tokens * HISTORY_SHARE * CHARS_PER_TOKEN)

    @wrap_model_call
    def guard(request, handler):
        messages, cut = fit_history(request.messages, budget)
        if cut:
            child._seen.clear()
            request = request.override(messages=messages)
        if request.model is not session._llm:
            request = request.override(model=session._llm)
        response = handler(request)
        msg = response if isinstance(response, AIMessage) else next(
            (m for m in reversed(getattr(response, "result", [])) if isinstance(m, AIMessage)), None)
        fixed = repair_tool_calls(msg, names) if msg is not None else None
        return fixed if fixed is not None else response

    return create_agent(session._llm, tools=tools, system_prompt=HELPER_PROMPT.format(environment=environment(child.root)),
                        middleware=[guard, *prov.agent_middleware(session._llm)])


def run(session: Session, task: str, number: int) -> str:
    from vesper.coder import split_thinking, text_of

    parent = session.workspace
    if not task.strip():
        return "Error: the task is empty. Describe what the helper should find out."

    def say(line: str) -> None:
        if parent.on_output is not None:
            try:
                parent.on_output(f"helper {number}: {line}")
            except Exception:  # noqa: BLE001 - showing progress must never break the helper
                pass

    with _slots:
        if parent.stopped:
            return "Error: the user stopped this request."
        child = _helper_workspace(parent)
        messages: list = []
        steps = 0
        out_of_steps = False
        try:
            agent = build(session, child)
            for update in agent.stream({"messages": [HumanMessage(task)]}, {"recursion_limit": 2 * MAX_STEPS + 1},
                                       stream_mode="updates"):
                for data in (update or {}).values():
                    for m in (data.get("messages", []) if isinstance(data, dict) else []):
                        messages.append(m)
                        if isinstance(m, AIMessage):
                            steps += 1
                            for call in m.tool_calls or []:
                                say(_label(call["name"], call.get("args", {})))
                if parent.stopped:
                    break
        except GraphRecursionError:
            out_of_steps = True
        except Exception as exc:  # noqa: BLE001 - the agent gets the reason and can carry on alone
            session._add_helper_usage(messages)
            return f"Error: the helper failed: {str(exc)[:300]}"
        finally:
            child.stopped = True
        session._add_helper_usage(messages)

    if parent.stopped:
        return "Error: the user stopped this request."
    reply = ""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and not m.tool_calls:
            reply = split_thinking(text_of(m.content))[1].strip()
            if reply:
                break
    if not reply:
        why = "ran out of steps" if out_of_steps else "gave no answer"
        return f"The helper {why} after {steps} steps. Ask a narrower question, or search yourself."
    if len(reply) > MAX_REPLY:
        reply = reply[:MAX_REPLY] + "\n... (the rest of the helper's reply was cut)"
    note = " It ran out of steps, so this may be incomplete." if out_of_steps else ""
    return f"{reply}\n\n(helper {number}: {steps} steps.{note})"
