"""MCP client: tools from Model Context Protocol servers, with the standard library only.

Servers are listed in `~/.vesper/mcp.json` (yours) and in a project's `.mcp.json` (the format
Claude Code, Cursor and others use), so a project set up for another harness works here too:

    {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
        "docs":   {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}
    }}

`command` servers talk JSON-RPC over stdin and stdout; `url` servers use Streamable HTTP.
`${NAME}` is filled from the environment (which includes `~/.vesper/.env`).

A project's servers start programs on this computer, so each one runs only after the user
approves it (`/mcp approve`); a changed definition needs a new approval. Every tool call goes
through the same approval as a shell command, except tools the server marks read-only.

When servers offer many tools, their full schemas would cost thousands of tokens on every
model call. Then the model gets two small tools instead, `mcp_find` and `mcp_call`, and looks
up the input of the one it needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL = "2025-06-18"
START_TIMEOUT_S = 60.0      # `npx -y package` downloads the server the first time
CALL_TIMEOUT_S = 120.0
DIRECT_TOOL_LIMIT = 12      # more tools than this: mcp_find / mcp_call instead of one tool each
MAX_RESULT_CHARS = 12_000
MAX_DESCRIPTION = 300
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class MCPError(Exception):
    pass


# -------------------------------------------------------------------- configuration
def _home() -> Path:
    return Path(os.environ.get("VESPER_HOME") or Path.home() / ".vesper")


def user_config_path() -> Path:
    return _home() / "mcp.json"


def _expand(value):
    """${NAME} from the environment; an unknown name becomes empty."""
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _read_servers(path: Path) -> tuple[dict, str | None]:
    """{name: definition} from one config file, and a problem to show (or None)."""
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"{path}: not valid JSON ({exc})"
    servers = data.get("mcpServers", data.get("servers")) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}, f'{path}: expected an object with "mcpServers"'
    good, bad = {}, []
    for name, spec in servers.items():
        if not isinstance(name, str) or not NAME.match(name) or not isinstance(spec, dict):
            bad.append(str(name))
        elif spec.get("disabled") is True:
            continue
        elif isinstance(spec.get("command"), str) or isinstance(spec.get("url"), str):
            good[name] = spec
        else:
            bad.append(name)
    return good, (f'{path}: skipped {", ".join(bad)} (each server needs a "command" or a "url")' if bad else None)


def fingerprint(path: Path, name: str, spec: dict) -> str:
    digest = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
    return f"{path.resolve()}::{name}::{digest}"


def find_project_config(root: str | Path) -> Path | None:
    """The nearest `.mcp.json` from the working folder up to the home folder."""
    here, home = Path(root).resolve(), Path.home().resolve()
    for folder in [here, *here.parents]:
        if folder == home:
            break  # ~/.mcp.json is not a project's file
        candidate = folder / ".mcp.json"
        if candidate.is_file():
            return candidate
    return None


def configured(root: str | Path) -> list[dict]:
    """Every server for this folder: name, scope (user/project), spec, file, fingerprint, trusted."""
    from vesper import permissions

    trusted = set(permissions.load_rules("mcp_trusted"))
    out, seen = [], set()
    project = find_project_config(root)
    sources = [(project, "project")] if project else []
    sources.append((user_config_path(), "user"))
    for path, scope in sources:
        servers, _problem = _read_servers(path)
        for name, spec in servers.items():
            if name in seen:  # the project's definition wins over the user's
                continue
            seen.add(name)
            fp = fingerprint(path, name, spec)
            out.append({"name": name, "scope": scope, "spec": spec, "file": str(path), "fingerprint": fp,
                        "trusted": scope == "user" or fp in trusted})
    return out


def problems(root: str | Path) -> list[str]:
    project = find_project_config(root)
    found = [_read_servers(p)[1] for p in ([project] if project else []) + [user_config_path()]]
    return [p for p in found if p]


def approve(root: str | Path, name: str | None = None) -> list[str]:
    """Trust a project's server (or all of them); returns the names approved."""
    from vesper import permissions

    names = []
    for server in configured(root):
        if server["scope"] == "project" and not server["trusted"] and (name is None or server["name"] == name):
            permissions.save_rule(server["fingerprint"], "mcp_trusted")
            names.append(server["name"])
    if names:
        manager().changed()
    return names


# -------------------------------------------------------------------- transports
class _Stdio:
    """JSON-RPC, one message per line, over a child process's stdin and stdout."""

    def __init__(self, name: str, spec: dict, cwd: str):
        command = [_expand(spec["command"]), *[str(a) for a in _expand(spec.get("args") or [])]]
        env = {**os.environ, **{str(k): str(v) for k, v in _expand(spec.get("env") or {}).items()}}
        log = _home() / "logs"
        log.mkdir(parents=True, exist_ok=True)
        self._stderr = open(log / f"mcp-{name}.log", "ab")  # noqa: SIM115 - closed in close()
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                                         cwd=_expand(spec.get("cwd")) or cwd, env=env, **flags)
        except OSError as exc:
            self._stderr.close()
            raise MCPError(f"could not start {command[0]}: {exc.strerror or exc}") from None
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        self.on_request = None  # (method, params) -> result, for requests the server sends us
        self.on_notice = None
        threading.Thread(target=self._read, daemon=True, name=f"mcp-{name}").start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue  # a server printing logs to stdout: ignore the line
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                self._answer(msg)
            elif "method" in msg:
                if self.on_notice:
                    self.on_notice(msg["method"])
            elif msg.get("id") in self._waiting:
                self._waiting.pop(msg["id"]).put(msg)
        for q in list(self._waiting.values()):
            q.put({"error": {"message": "the server stopped"}})
        self._waiting.clear()

    def _answer(self, msg: dict) -> None:
        try:
            result = self.on_request(msg["method"], msg.get("params") or {}) if self.on_request else None
            reply = ({"jsonrpc": "2.0", "id": msg["id"], "result": result} if result is not None else
                     {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
        except Exception as exc:  # noqa: BLE001 - answer the server rather than leave it waiting
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(exc)}}
        try:
            self._send(reply)
        except MCPError:
            pass

    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        with self._lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise MCPError("the server stopped") from None

    def request(self, rid: int, method: str, params: dict, timeout: float) -> dict:
        q: queue.Queue = queue.Queue(maxsize=1)
        self._waiting[rid] = q
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return q.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"no answer to {method} in {timeout:.0f} s") from None
        finally:
            self._waiting.pop(rid, None)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        self._stderr.close()


class _Http:
    """Streamable HTTP: each message is a POST; the answer is JSON or a short event stream."""

    def __init__(self, name: str, spec: dict, cwd: str):
        if spec.get("type") == "sse":
            raise MCPError("the old SSE transport is not supported; use the server's Streamable HTTP address")
        self.url = _expand(spec["url"])
        if not re.match(r"^https?://", self.url):
            raise MCPError("url must start with http:// or https://")
        self.headers = {str(k): str(v) for k, v in _expand(spec.get("headers") or {}).items()}
        self.session_id = None
        self.version = None
        self.on_request = None
        self.on_notice = None

    def _open(self, req, timeout: float):
        from vesper.webtools import _tls

        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_tls() if self.url.startswith("https") else None)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MCPError(f'the server refused the credentials (HTTP {exc.code}); check "headers"') from None
            raise MCPError(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"could not reach {self.url}: {getattr(exc, 'reason', exc)}") from None

    def _post(self, msg: dict, timeout: float, rid: int | None = None) -> dict | None:
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        req = urllib.request.Request(self.url, json.dumps(msg).encode(), headers, method="POST")
        with self._open(req, timeout) as resp:
            self.session_id = resp.headers.get("Mcp-Session-Id") or self.session_id
            if rid is None:
                return None
            if "text/event-stream" in resp.headers.get("Content-Type", ""):
                data: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        try:
                            got = json.loads("\n".join(data))
                        except ValueError:
                            got = None
                        data = []
                        if isinstance(got, dict) and got.get("id") == rid and "method" not in got:
                            return got
                raise MCPError("the event stream ended without an answer")
            try:
                return json.loads(resp.read() or b"{}")
            except ValueError:
                raise MCPError("the server did not answer with JSON") from None

    def request(self, rid: int, method: str, params: dict, timeout: float) -> dict:
        return self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, timeout, rid)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 15)

    def close(self) -> None:
        if self.session_id:
            try:
                req = urllib.request.Request(self.url, headers={**self.headers, "Mcp-Session-Id": self.session_id},
                                             method="DELETE")
                self._open(req, 3).close()
            except Exception:  # noqa: BLE001 - the server cleans up on its own
                pass


# -------------------------------------------------------------------- one server
class Server:
    def __init__(self, name: str, spec: dict, root: str):
        self.name, self.spec, self.root = name, spec, root
        self.status, self.error, self.tools = "starting", None, []
        self.info: dict = {}
        self._next = 0
        self._id_lock = threading.Lock()
        self.timeout = float(spec.get("timeout") or CALL_TIMEOUT_S)
        self.conn = None

    def _rid(self) -> int:
        with self._id_lock:
            self._next += 1
            return self._next

    def _call(self, method: str, params: dict, timeout: float) -> dict:
        reply = self.conn.request(self._rid(), method, params, timeout)
        if "error" in reply:
            err = reply["error"] or {}
            raise MCPError(str(err.get("message") if isinstance(err, dict) else err)[:500])
        return reply.get("result") or {}

    def start(self) -> None:
        try:
            from vesper import __version__

            transport = _Http if "url" in self.spec else _Stdio
            self.conn = transport(self.name, self.spec, self.root)
            self.conn.on_request = self._server_request
            self.conn.on_notice = self._notice
            result = self._call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {"roots": {}},
                                               "clientInfo": {"name": "vesper", "version": __version__}},
                                float(self.spec.get("startup_timeout") or START_TIMEOUT_S))
            if isinstance(self.conn, _Http):
                self.conn.version = result.get("protocolVersion") or PROTOCOL
            self.info = result.get("serverInfo") or {}
            self.conn.notify("notifications/initialized")
            self.tools = self._list_tools()
            self.status = "ready"
        except Exception as exc:  # noqa: BLE001 - one broken server must not stop the others
            self.error = str(exc)[:500] or type(exc).__name__
            self.status = "failed"
            self.close()

    def _list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _page in range(50):
            result = self._call("tools/list", {"cursor": cursor} if cursor else {}, 30)
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def _server_request(self, method: str, params: dict):
        if method == "ping":
            return {}
        if method == "roots/list":
            return {"roots": [{"uri": Path(self.root).resolve().as_uri(), "name": Path(self.root).name or "root"}]}
        return None

    def _notice(self, method: str) -> None:
        if method == "notifications/tools/list_changed" and self.status == "ready":
            threading.Thread(target=self._reload_tools, daemon=True).start()

    def _reload_tools(self) -> None:
        try:
            self.tools = self._list_tools()
            manager().changed()
        except Exception:  # noqa: BLE001 - keep the old list
            pass

    def call(self, tool: str, args: dict) -> str:
        if self.status != "ready" or self.conn is None:
            raise MCPError(f"server {self.name} is {self.status}" + (f": {self.error}" if self.error else ""))
        result = self._call("tools/call", {"name": tool, "arguments": args}, self.timeout)
        text = result_text(result)
        return ("Error: " + text) if result.get("isError") else text

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self.status == "ready":
            self.status = "stopped"


def result_text(result: dict) -> str:
    """A tool result as text: text parts in order, other parts as short notes."""
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {item.get('mimeType', '')}, {len(item.get('data') or '') * 3 // 4:,} bytes]")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(str(res["text"]) if "text" in res else f"[resource {res.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[link {item.get('uri', '')}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts).strip() or "(no output)"
    if len(text) > MAX_RESULT_CHARS:
        half = MAX_RESULT_CHARS // 2
        text = f"{text[:half]}\n… ({len(text) - MAX_RESULT_CHARS:,} characters cut) …\n{text[-half:]}"
    return text


# -------------------------------------------------------------------- all servers
class Manager:
    """The running servers of this process, shared by every chat."""

    def __init__(self):
        self.servers: dict[str, Server] = {}   # key: fingerprint
        self._lock = threading.Lock()
        self.version = 0

    def changed(self) -> None:
        self.version += 1

    def for_root(self, root: str, extra: list[dict] | tuple = ()) -> list[Server]:
        """Start (once) every trusted server for this folder; returns them, ready or not.

        `extra` are servers a client handed over for one session ({"name", "spec"}, from an
        editor over ACP). The user set them up in that client, so they count as trusted; a
        configured server of the same name wins.
        """
        wanted = []
        items = [i for i in configured(root) if i["trusted"]]
        names = {i["name"] for i in items}
        for e in extra:
            if e["name"] not in names:
                names.add(e["name"])
                items.append({"name": e["name"], "spec": e["spec"],
                              "fingerprint": fingerprint(Path("acp-client"), e["name"], e["spec"])})
        with self._lock:
            for item in items:
                server = self.servers.get(item["fingerprint"])
                if server is None or server.status in ("failed", "stopped") and server.root != str(root):
                    server = Server(item["name"], item["spec"], str(root))
                    self.servers[item["fingerprint"]] = server
                    threading.Thread(target=self._start, args=(server,), daemon=True,
                                     name=f"mcp-start-{item['name']}").start()
                wanted.append(server)
        return wanted

    def restart(self, root: str, name: str | None = None) -> None:
        with self._lock:
            gone = [k for k, s in self.servers.items() if name is None or s.name == name]
            servers = [self.servers.pop(k) for k in gone]
        for s in servers:
            s.close()
        self.changed()
        self.for_root(root)

    def _start(self, server: Server) -> None:
        server.start()
        self.changed()

    @staticmethod
    def wait(servers: list[Server], timeout: float) -> None:
        end = time.monotonic() + timeout
        while any(s.status == "starting" for s in servers) and time.monotonic() < end:
            time.sleep(0.05)

    def close(self) -> None:
        with self._lock:
            servers, self.servers = list(self.servers.values()), {}
        for s in servers:
            s.close()


_manager: Manager | None = None
_manager_lock = threading.Lock()


def manager() -> Manager:
    global _manager
    with _manager_lock:
        if _manager is None:
            import atexit

            _manager = Manager()
            atexit.register(_manager.close)
        return _manager


def status(root: str | Path) -> list[dict]:
    """What the app and /mcp show: every configured server and how it is doing."""
    running = {fp: s for fp, s in manager().servers.items()}
    out = []
    for item in configured(root):
        s = running.get(item["fingerprint"])
        state = s.status if s else ("needs approval" if not item["trusted"] else "not started")
        out.append({"name": item["name"], "scope": item["scope"], "file": item["file"], "status": state,
                    "error": s.error if s else None, "tools": [t["name"] for t in s.tools] if s else [],
                    "transport": "http" if "url" in item["spec"] else "stdio"})
    return out


# -------------------------------------------------------------------- tools for the model
def _short(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 1] + "…"


def _schema(tool: dict) -> dict:
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    schema = {**schema, "type": "object"}
    props = {}
    for key, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict) and "description" in prop:
            prop = {**prop, "description": _short(prop["description"])}
        props[key] = prop
    return {**schema, "properties": props}


def read_only(tool: dict) -> bool:
    return bool((tool.get("annotations") or {}).get("readOnlyHint"))


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def lookup(servers: list[Server], server: str, tool: str) -> tuple[Server, dict]:
    s = next((x for x in servers if x.name == server), None)
    if s is None:
        raise MCPError(f"no MCP server named {server!r}; servers: {', '.join(x.name for x in servers) or 'none'}")
    t = next((x for x in s.tools if x["name"] == tool), None)
    if t is None:
        raise MCPError(f"server {server} has no tool {tool!r}; use mcp_find to see its tools")
    return s, t


def langchain_tools(servers: list[Server], call) -> list:
    """LangChain tools for the ready servers.

    `call(server, tool, args_json, step)` runs one with approval; `step` is the name of the
    LangChain tool the model called, which the app uses to put the approval on that step.
    """
    from langchain_core.tools import StructuredTool

    ready = [s for s in servers if s.status == "ready"]
    every = [(s, t) for s in ready for t in s.tools]
    if not every:
        return []
    if len(every) <= DIRECT_TOOL_LIMIT:
        out = []
        for s, t in every:
            def run(_s=s.name, _t=t["name"], **kwargs):
                return call(_s, _t, json.dumps(kwargs, ensure_ascii=False, sort_keys=True), tool_name(_s, _t))

            out.append(StructuredTool(name=tool_name(s.name, t["name"]), args_schema=_schema(t), func=run,
                                      description=f"[{s.name}] " + _short(t.get("description") or t["name"])))
        return out

    names = ", ".join(sorted({s.name for s in ready}))

    def mcp_find(query: str = "") -> str:
        words = query.lower().split()
        hits = [(s, t) for s, t in every
                if all(w in f"{s.name} {t['name']} {t.get('description', '')}".lower() for w in words)]
        if not hits:
            return "No tool matches. Try fewer words, or an empty query for the whole list."
        lines = [f"{s.name} / {t['name']}: {_short(t.get('description') or '')}\n  input: "
                 f"{json.dumps(_schema(t).get('properties'), ensure_ascii=False)[:1500]}" for s, t in hits[:8]]
        return "\n".join(lines) + (f"\n({len(hits) - 8} more; add words to narrow it)" if len(hits) > 8 else "")

    def mcp_call(server: str, tool: str, arguments: dict | None = None) -> str:
        return call(server, tool, json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True), "mcp_call")

    return [
        StructuredTool.from_function(mcp_find, name="mcp_find",
                                     description=f"Find tools of the MCP servers ({names}) by words; shows each one's input."),
        StructuredTool.from_function(mcp_call, name="mcp_call",
                                     description="Run an MCP tool found with mcp_find: server, tool name and its arguments."),
    ]
