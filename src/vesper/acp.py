"""ACP: Vesper as the agent inside editors that speak the Agent Client Protocol.

`vesper acp` reads JSON-RPC 2.0 messages from stdin and writes them to stdout, one JSON object
per line (https://agentclientprotocol.com). An editor such as Zed starts it as an external
agent, opens sessions in a folder and sends prompts; Vesper streams back its answer, its
reasoning, every tool step and its task list, and asks the editor before edits and commands
the way the terminal app asks the user.

What maps to what:
- session/new, session/load: a `Session` with its own `Workspace`, saved in the same database
  as the terminal and the desktop app (source "acp"). Loading replays the saved turns.
- session/prompt: text, embedded files and images go to `Session.ask`; `/name` runs a custom
  command. The reply ends with stopReason end_turn, or cancelled after session/cancel.
- session/set_mode: the approval modes (ask, auto-edit, yolo, plan).
- Approvals become session/request_permission (allow once, allow always, reject).
- mcpServers: the editor's stdio and HTTP MCP servers start for that session, next to the
  ones in Vesper's own mcp.json (a configured server of the same name wins).

Nothing but protocol messages may reach stdout, so `main` points sys.stdout at stderr before
anything else runs.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_TOOL_OUTPUT = 8000       # characters of a tool's output sent to the editor
PERMISSION_TIMEOUT = 3600    # seconds an approval may wait for the editor

MODES = [
    {"id": "ask", "name": "Ask", "description": "Ask before edits and commands"},
    {"id": "auto-edit", "name": "Auto-edit", "description": "Edit files freely, ask before commands"},
    {"id": "yolo", "name": "Yolo", "description": "Do everything without asking"},
    {"id": "plan", "name": "Plan", "description": "Read and plan only, change nothing"},
]
MODE_IDS = {m["id"] for m in MODES}

KINDS = {
    "read_file": "read", "list_files": "read", "glob": "search", "grep": "search",
    "write_file": "edit", "edit_file": "edit", "remember": "edit", "run_command": "execute",
    "web_fetch": "fetch", "web_search": "fetch", "subagent": "think", "update_tasks": "think",
}
PLAN_STATUS = {"pending": "pending", "active": "in_progress", "done": "completed"}


def mcp_specs(servers) -> list[dict]:
    """The editor's mcpServers as Vesper's MCP specs: [{"name", "spec"}]. SSE servers are left out."""
    out = []
    for s in servers if isinstance(servers, list) else []:
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            continue
        pairs = s.get("headers") if "url" in s else s.get("env")
        table = {str(p.get("name")): str(p.get("value", "")) for p in pairs or [] if isinstance(p, dict) and p.get("name")}
        if s.get("type") == "http" and s.get("url"):
            out.append({"name": str(s["name"]), "spec": {"url": str(s["url"]), "headers": table}})
        elif s.get("command") and s.get("type") in (None, "stdio"):
            out.append({"name": str(s["name"]), "spec": {"command": str(s["command"]),
                                                         "args": [str(a) for a in s.get("args") or []], "env": table}})
    return out


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def tool_title(name: str, args: dict) -> str:
    """A short line naming the step, like the terminal's tool line."""
    try:
        from vesper import ui

        return ui.step_title(name, args)
    except Exception:  # noqa: BLE001
        return name


def _abs(root: Path, path: str) -> Path:
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else Path(root) / p


def tool_locations(root: Path, args: dict) -> list[dict]:
    path = args.get("path") if isinstance(args, dict) else None
    return [{"path": str(_abs(root, path))}] if isinstance(path, str) and path else []


def edit_diff(root: Path, name: str, args: dict) -> dict | None:
    """The ACP diff for a write or an edit the agent is about to make, or None."""
    try:
        path = _abs(root, str(args.get("path", "")))
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if name == "write_file":
            new = str(args.get("content", ""))
        elif name == "edit_file" and old is not None:
            o, n = str(args.get("old_string", "")), str(args.get("new_string", ""))
            if not o or o not in old:
                return None
            new = old.replace(o, n) if args.get("replace_all") else old.replace(o, n, 1)
        else:
            return None
        return {"type": "diff", "path": str(path), "oldText": old, "newText": new}
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def text_block(text: str) -> dict:
    return {"type": "content", "content": {"type": "text", "text": text}}


def prompt_parts(blocks) -> tuple[str, list]:
    """(the message text, image blocks) from an ACP prompt."""
    import base64
    import binascii

    from vesper import attachments as att

    texts, images = [], []
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            texts.append(str(b.get("text", "")))
        elif kind == "resource":
            res = b.get("resource") or {}
            if isinstance(res, dict) and "text" in res:
                path = str(res.get("uri", "")).removeprefix("file://")
                texts.append(f'<file path="{path}">\n{res.get("text", "")}\n</file>')
        elif kind == "resource_link":
            uri = str(b.get("uri", ""))
            texts.append(f"(file: {uri.removeprefix('file://')})" if uri.startswith("file://") else f"({uri})")
        elif kind == "image" and len(images) < att.MAX_IMAGES and b.get("mimeType") in att.IMAGE_TYPES.values():
            try:
                data = base64.b64decode(str(b.get("data", "")), validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(data) <= att.MAX_IMAGE_BYTES:
                images.append(att.image_block(data, b["mimeType"], "image"))
    return "\n\n".join(t for t in texts if t), images


class Connection:
    """JSON-RPC over two byte streams: replies, notifications, and requests to the editor."""

    def __init__(self, inp, out):
        self.inp, self.out = inp, out
        self._write = threading.Lock()
        self._next = 0
        self._waiting: dict[int, dict] = {}
        self._waiting_lock = threading.Lock()
        self.closed = threading.Event()

    def send(self, msg: dict) -> None:
        data = (json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._write:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):
                pass  # the editor went away; the read loop ends on its own

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float = PERMISSION_TIMEOUT, cancel=None) -> dict:
        """Ask the editor and wait for its answer. Raises RpcError on an error, a timeout or a cancel."""
        with self._waiting_lock:
            self._next += 1
            rid = self._next
            slot = {"event": threading.Event(), "reply": None}
            self._waiting[rid] = slot
        try:
            self.send({"id": rid, "method": method, "params": params})
            waited = 0.0
            while not slot["event"].wait(0.2):
                waited += 0.2
                if waited >= timeout or self.closed.is_set() or (cancel is not None and cancel.is_set()):
                    raise RpcError(-32000, "no answer")
            reply = slot["reply"]
            if not isinstance(reply, dict):
                raise RpcError(-32000, "no answer")
            if isinstance(reply.get("error"), dict):
                raise RpcError(int(reply["error"].get("code", -32000)), str(reply["error"].get("message", "")))
            return reply.get("result") or {}
        finally:
            with self._waiting_lock:
                self._waiting.pop(rid, None)

    def answered(self, msg: dict) -> None:
        with self._waiting_lock:
            slot = self._waiting.get(msg.get("id"))
        if slot is not None:
            slot["reply"] = msg
            slot["event"].set()

    def close(self) -> None:
        self.closed.set()
        with self._waiting_lock:
            for slot in self._waiting.values():
                slot["event"].set()


class AcpSession:
    """One editor session: a Vesper `Session` and what is needed to talk about it."""

    def __init__(self, session, conn: Connection):
        self.session = session
        self.conn = conn
        self.lock = threading.Lock()       # one prompt at a time
        self.cancelled = threading.Event()
        self.open_tools: dict[str, str] = {}  # running tool call id: tool name, for approvals

    @property
    def id(self) -> str:
        return self.session.id

    def update(self, update: dict) -> None:
        self.conn.notify("session/update", {"sessionId": self.id, "update": update})

    def approve(self, action: dict) -> str:
        """A Workspace approval, asked in the editor: "yes", "always" or "no"."""
        if self.cancelled.is_set():
            return "no"
        name = action.get("tool", "")
        call_id = next((cid for cid, n in reversed(list(self.open_tools.items())) if n == name), None)
        args = {k: v for k, v in action.items() if k in ("path", "command", "url", "server", "mcp_tool")}
        tool_call: dict = {"toolCallId": call_id or f"approval-{id(action)}", "title": tool_title(name, args),
                           "kind": KINDS.get(name, "other"), "status": "pending"}
        if action.get("diff") and name not in ("write_file", "edit_file"):
            tool_call["content"] = [text_block(f"```diff\n{action['diff']}\n```")]
        options = [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}]
        if not action.get("strict"):
            options.append({"optionId": "always", "name": "Always allow", "kind": "allow_always"})
        options.append({"optionId": "no", "name": "Reject", "kind": "reject_once"})
        try:
            result = self.conn.request("session/request_permission",
                                       {"sessionId": self.id, "toolCall": tool_call, "options": options},
                                       cancel=self.cancelled)
        except RpcError:
            return "no"
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
            return "no"
        return outcome.get("optionId") if outcome.get("optionId") in ("yes", "always") else "no"

    def send_event(self, kind: str, data: dict) -> None:
        """One `Session.ask` event as the matching session/update, if it has one."""
        root = self.session.workspace.root
        if kind == "token" and data.get("text"):
            self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": data["text"]}})
        elif kind == "reasoning" and data.get("text"):
            self.update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": data["text"]}})
        elif kind == "tool_start":
            name, args = data.get("name", ""), data.get("args") or {}
            cid = str(data.get("id") or f"tool-{len(self.open_tools)}")
            self.open_tools[cid] = name
            update = {"sessionUpdate": "tool_call", "toolCallId": cid, "title": tool_title(name, args),
                      "kind": KINDS.get(name, "other"), "status": "in_progress", "rawInput": args,
                      "locations": tool_locations(root, args)}
            if name in ("write_file", "edit_file"):
                diff = edit_diff(root, name, args)
                if diff:
                    update["content"] = [diff]
            self.update(update)
        elif kind == "tool_end":
            cid = str(data.get("id") or "")
            name = self.open_tools.pop(cid, data.get("name", ""))
            out = str(data.get("output", ""))
            if len(out) > MAX_TOOL_OUTPUT:
                out = out[:MAX_TOOL_OUTPUT] + "\n... (cut)"
            update = {"sessionUpdate": "tool_call_update", "toolCallId": cid,
                      "status": "completed" if data.get("ok") else "failed", "rawOutput": {"output": out}}
            if name not in ("write_file", "edit_file") or not data.get("ok"):
                update["content"] = [text_block(out)] if out else []  # an edit keeps showing its diff
            self.update(update)
        elif kind == "tasks":
            self.update({"sessionUpdate": "plan", "entries": [
                {"content": str(t.get("text", "")), "priority": "medium",
                 "status": PLAN_STATUS.get(t.get("status"), "pending")} for t in data.get("items") or []]})
        elif kind == "notice" and data.get("text"):
            self.update({"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": f"\n\n_{data['text']}_\n\n"}})

    def replay(self) -> None:
        """Send a loaded conversation to the editor, turn by turn."""
        from vesper.coder import transcript

        for n, turn in enumerate(transcript(self.session.history)):
            if turn["role"] == "user":
                self.update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": turn["text"]}})
                continue
            for i, part in enumerate(turn["parts"]):
                if part["type"] == "tool":
                    args = part.get("args") or {}
                    out = str(part.get("output") or "")[:MAX_TOOL_OUTPUT]
                    self.update({"sessionUpdate": "tool_call", "toolCallId": part["id"] or f"saved-{n}-{i}",
                                 "title": tool_title(part["name"], args), "kind": KINDS.get(part["name"], "other"),
                                 "status": "failed" if part.get("ok") is False else "completed",
                                 "rawInput": args, "content": [text_block(out)] if out else []})
                else:
                    kind = "agent_thought_chunk" if part["type"] == "reasoning" else "agent_message_chunk"
                    self.update({"sessionUpdate": kind, "content": {"type": "text", "text": part["text"]}})


class Agent:
    """The agent side of ACP: answers the editor's requests."""

    METHODS = {
        "initialize": "initialize", "authenticate": "authenticate", "session/new": "session_new",
        "session/load": "session_load", "session/prompt": "session_prompt", "session/set_mode": "session_set_mode",
    }
    SLOW = ("session/new", "session/load", "session/prompt")  # answered from their own thread

    def __init__(self, conn: Connection, store=None, cfg: dict | None = None, make_llm=None):
        self.conn = conn
        self.store = store
        self.cfg = cfg            # tests pass their own; otherwise config.json, read for each session
        self.make_llm = make_llm
        self.sessions: dict[str, AcpSession] = {}
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def _config(self) -> dict:
        if self.cfg is not None:
            return self.cfg
        from vesper.onboarding import load_config

        return load_config()

    def _llm(self, cfg: dict):
        if self.make_llm is not None:
            return self.make_llm(cfg)
        from vesper import providers as prov

        return prov.get_llm(cfg.get("provider"), cfg.get("model") or None, thinking=cfg.get("thinking") or "off")

    def _open(self, cwd: str, session_id: str | None = None, mcp_servers=None) -> AcpSession:
        from vesper import permissions
        from vesper import providers as prov
        from vesper.coder import Session
        from vesper.workspace import Workspace

        cfg = self._config()
        if not prov.fake_llm_enabled() and not cfg.get("provider"):
            raise RpcError(-32000, "No model is set up yet. Run `vesper` in a terminal once to choose one.")
        root = Path(cwd).expanduser()
        if not root.is_dir():
            raise RpcError(-32602, f"cwd is not a folder: {cwd}")
        mode = cfg.get("agent_mode") if cfg.get("agent_mode") in MODE_IDS else "ask"
        ws = Workspace(root, mode=mode, rules=permissions.load_rules("allow_commands"),
                       hosts=permissions.load_rules("allow_hosts"),
                       on_rule=lambda key, rule: permissions.save_rule(rule, key))
        session = Session(self._llm(cfg), ws, self.store, session_id=session_id, source="acp",
                          provider=cfg.get("provider", ""), model=cfg.get("model", ""),
                          context_tokens=prov.context_tokens(cfg.get("provider"), cfg.get("model")))
        session.extra_mcp = mcp_specs(mcp_servers)
        acp = AcpSession(session, self.conn)
        ws.approve = acp.approve
        with self._sessions_lock:
            self.sessions[session.id] = acp
        return acp

    def _session(self, params: dict) -> AcpSession:
        acp = self.sessions.get(str(params.get("sessionId", "")))
        if acp is None:
            raise RpcError(-32002, "Unknown session")
        return acp

    @staticmethod
    def _modes(acp: AcpSession) -> dict:
        return {"currentModeId": acp.session.workspace.mode, "availableModes": MODES}

    @staticmethod
    def _send_commands(acp: AcpSession) -> None:
        """The project's and the user's custom slash commands, for the editor's / menu."""
        try:
            from vesper.commands import find_commands

            found = find_commands(acp.session.workspace.root)
        except Exception:  # noqa: BLE001
            return
        commands = [{"name": name, "description": str(c.get("description") or f"Custom command /{name}")[:200],
                     "input": {"hint": "arguments"}} for name, c in sorted(found.items())]
        if commands:
            acp.update({"sessionUpdate": "available_commands_update", "availableCommands": commands})

    # ------------------------------------------------------------ methods
    def initialize(self, params: dict) -> dict:
        from vesper import __version__

        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": self.store is not None,
                "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": True, "sse": False},
            },
            "agentInfo": {"name": "vesper", "title": "Vesper", "version": __version__},
            "authMethods": [],
        }

    def authenticate(self, params: dict) -> dict:
        return {}

    def session_new(self, params: dict) -> dict:
        acp = self._open(str(params.get("cwd") or os.getcwd()), mcp_servers=params.get("mcpServers"))
        timer = threading.Timer(0.1, self._send_commands, args=(acp,))  # once the editor knows the id
        timer.daemon = True
        timer.start()
        return {"sessionId": acp.id, "modes": self._modes(acp)}

    def session_load(self, params: dict) -> dict:
        if self.store is None:
            raise RpcError(-32000, "Session history is off, so saved sessions cannot be loaded.")
        row = self.store.get_session(str(params.get("sessionId", "")))
        if row is None:
            raise RpcError(-32002, "Unknown session")
        acp = self.sessions.get(row["id"]) or self._open(str(params.get("cwd") or row["cwd"]), row["id"],
                                                          params.get("mcpServers"))
        acp.replay()
        self._send_commands(acp)
        return {"modes": self._modes(acp)}

    def session_set_mode(self, params: dict) -> dict:
        acp = self._session(params)
        mode = params.get("modeId")
        if mode not in MODE_IDS:
            raise RpcError(-32602, f"Unknown mode: {mode}")
        acp.session.workspace.mode = mode
        return {}

    def session_cancel(self, params: dict) -> None:
        acp = self.sessions.get(str(params.get("sessionId", "")))
        if acp is not None:
            acp.cancelled.set()
            acp.session.workspace.cancel()

    def session_prompt(self, params: dict) -> dict:
        acp = self._session(params)
        if not acp.lock.acquire(blocking=False):
            raise RpcError(-32000, "This session is still answering.")
        try:
            acp.cancelled.clear()
            text, images = prompt_parts(params.get("prompt"))
            if not text and not images:
                return {"stopReason": "end_turn"}
            shown = None
            if text.startswith("/"):
                from vesper.commands import expand

                command = expand(acp.session.workspace.root, text)
                if command:
                    shown, text = text, command
            acp.session.emit = acp.send_event  # checks and hooks show while they run
            for kind, data in acp.session.ask(text, shown=shown, images=images or None):
                if acp.cancelled.is_set():
                    break
                acp.send_event(kind, data)
            return {"stopReason": "cancelled" if acp.cancelled.is_set() else "end_turn"}
        except Exception as exc:  # noqa: BLE001 - the editor shows the reason as the answer
            if acp.cancelled.is_set():
                return {"stopReason": "cancelled"}
            from vesper.server import friendly_error

            acp.update({"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": friendly_error(exc, self._config().get("provider", ""))}})
            return {"stopReason": "end_turn"}
        finally:
            acp.open_tools.clear()
            acp.lock.release()

    # ------------------------------------------------------------ the loop
    def handle(self, msg: dict) -> threading.Thread | None:
        """One message from the editor. Returns the thread a slow request runs in."""
        method = msg.get("method")
        if method is None:
            self.conn.answered(msg)  # the editor's answer to one of our requests
            return None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "session/cancel":
            self.session_cancel(params)
            return None
        rid = msg.get("id")
        if rid is None:
            return None  # a notification we do not know
        name = self.METHODS.get(method)

        def run():
            try:
                if name is None:
                    raise RpcError(-32601, f"Method not found: {method}")
                self.conn.send({"id": rid, "result": getattr(self, name)(params)})
            except RpcError as exc:
                self.conn.send({"id": rid, "error": {"code": exc.code, "message": str(exc)}})
            except Exception as exc:  # noqa: BLE001
                self.conn.send({"id": rid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

        if method in self.SLOW:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            return thread
        run()
        return None

    def serve(self) -> None:
        """Read messages until the editor closes stdin, then stop every running turn."""
        try:
            for raw in iter(self.conn.inp.readline, b""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.conn.send({"id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if isinstance(msg, dict):
                    self.handle(msg)
        finally:
            self.conn.close()
            for acp in list(self.sessions.values()):
                acp.cancelled.set()
                acp.session.workspace.cancel()


def main() -> int:
    """`vesper acp`: serve ACP on stdin and stdout until the editor closes them."""
    out, inp = sys.stdout.buffer, sys.stdin.buffer
    sys.stdout = sys.stderr  # a stray print must never break the protocol
    store = None
    try:
        from vesper.store import Store

        store = Store()
    except Exception as exc:  # noqa: BLE001
        print(f"vesper acp: session history is off ({exc})", file=sys.stderr)
    try:
        Agent(Connection(inp, out), store=store).serve()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            from vesper import diagnostics

            diagnostics.manager().close()
        except Exception:  # noqa: BLE001
            pass
    return 0
