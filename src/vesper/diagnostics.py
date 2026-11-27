"""Type errors from a language server, right after the agent edits a file.

When the agent writes or edits a file, Vesper asks the language server for that language
(pyright or basedpyright, typescript-language-server, gopls, clangd) what is wrong with it,
and adds the errors the edit introduced to the tool result. The agent sees a broken import
or a wrong argument at once, instead of three steps later.

Only servers already installed are used (on PATH, in the project's `.venv` or
`node_modules/.bin`); nothing is installed. Servers start on the first edit of their language
and stay running for the session. Errors already in the file before the edit are left out, so
a messy file does not send the agent off fixing unrelated code. Every failure is silent: a
missing, slow or crashing server adds nothing and never turns a good write into an error.

Standard library only. Config key `diagnostics` (default on), `/diagnostics` in the terminal.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

FIRST_WAIT = 8.0   # seconds for a server that is still starting (startup plus the first analysis)
WAIT = 3.0         # seconds for a server already running
SETTLE = 0.15      # after the first report, how long to wait for a later one that replaces it
MAX_LINES = 10
MAX_CHARS = 1500
MAX_SERVERS = 4    # the least recently used one stops when a fifth starts


@dataclass(frozen=True)
class Spec:
    name: str
    exts: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]  # first one found wins
    markers: tuple[str, ...]               # files that mark the project root

    def language(self, path: Path) -> str:
        return _LANGUAGE.get(path.suffix.lower(), self.name)


SPECS = (
    Spec("pyright", (".py", ".pyi"),
         (("basedpyright-langserver", "--stdio"), ("pyright-langserver", "--stdio")),
         ("pyrightconfig.json", "pyproject.toml", "setup.py", "setup.cfg", ".git")),
    Spec("typescript", (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
         (("typescript-language-server", "--stdio"),),
         ("tsconfig.json", "jsconfig.json", "package.json", ".git")),
    Spec("gopls", (".go",), (("gopls",),), ("go.mod", ".git")),
    Spec("clangd", (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"), (("clangd",),),
         ("compile_commands.json", "CMakeLists.txt", ".git")),
)

_LANGUAGE = {
    ".py": "python", ".pyi": "python", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "typescriptreact", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascriptreact", ".go": "go", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}


def enabled() -> bool:
    try:
        from .onboarding import load_config

        return load_config().get("diagnostics", True) is not False
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    from .onboarding import save_config

    save_config({"diagnostics": bool(on)})
    if not on:
        manager().close()


def spec_for(path: Path) -> Spec | None:
    ext = path.suffix.lower()
    return next((s for s in SPECS if ext in s.exts), None)


def project_root(path: Path, spec: Spec) -> Path:
    """The nearest folder above `path` holding one of the spec's marker files."""
    for folder in path.parents:
        if any((folder / m).exists() for m in spec.markers):
            return folder
    return path.parent


def _venv_bin(root: Path) -> list[Path]:
    sub = "Scripts" if os.name == "nt" else "bin"
    return [root / v / sub for v in (".venv", "venv")]


def find_command(spec: Spec, root: Path) -> list[str] | None:
    """The server command for this project: its own install first, then PATH."""
    local = [*_venv_bin(root), root / "node_modules" / ".bin"]
    for cmd in spec.commands:
        for folder in local:
            if folder.is_dir():
                hit = shutil.which(cmd[0], path=str(folder))
                if hit:
                    return [hit, *cmd[1:]]
        hit = shutil.which(cmd[0])
        if hit:
            return [hit, *cmd[1:]]
    return None


def python_for(root: Path) -> str | None:
    name = "python.exe" if os.name == "nt" else "python"
    return next((str(b / name) for b in _venv_bin(root) if (b / name).is_file()), None)


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _uri_key(uri: str) -> str:
    """Servers may spell a file URI differently (drive letter case, escaping): compare paths."""
    raw = unquote(urlparse(uri).path)
    return _key(url2pathname(raw) if os.name == "nt" else raw)


# ---------------------------------------------------------------------------- one server
class Server:
    """One language server process for one project root, spoken to over stdio."""

    def __init__(self, spec: Spec, command: list[str], root: Path):
        self.spec, self.root, self.command = spec, root, command
        self.label = Path(command[0]).stem.removesuffix("-langserver")  # basedpyright, gopls, ...
        self.used = time.monotonic()
        self.dead = False
        self._lock = threading.Lock()   # one document sync at a time
        self._write = threading.Lock()
        self._cond = threading.Condition()
        self._next = 0
        self._replies: dict[int, queue.Queue] = {}
        self._versions: dict[str, int] = {}
        self._reports: dict[str, tuple[int, int | None, list]] = {}  # path key: (count, version, diagnostics)
        self.proc = subprocess.Popen(
            command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "NO_COLOR": "1"},
            **({"start_new_session": True} if os.name == "posix" else
               {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}))
        threading.Thread(target=self._read, daemon=True, name=f"lsp-{spec.name}").start()

    # --- framing
    def _send(self, msg: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
        with self._write:
            self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.proc.stdin.flush()

    def _read(self) -> None:
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    name, _, value = line.decode("ascii", "replace").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                body = out.read(length)
                if len(body) < length:
                    return
                try:
                    self._handle(json.loads(body))
                except Exception:
                    continue
        except Exception:
            return
        finally:
            self.dead = True
            with self._cond:
                self._cond.notify_all()
            for q in list(self._replies.values()):
                q.put(None)

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        if method and "id" in msg:  # the server asks something: always answer, or it may stall
            self._send({"id": msg["id"], "result": self._answer(method, msg.get("params") or {})})
        elif "id" in msg:
            q = self._replies.get(msg["id"])
            if q:
                q.put(msg)
        elif method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            key = _uri_key(params.get("uri", ""))
            with self._cond:
                count = self._reports.get(key, (0, None, []))[0] + 1
                self._reports[key] = (count, params.get("version"), params.get("diagnostics") or [])
                self._cond.notify_all()

    def _answer(self, method: str, params: dict):
        if method == "workspace/configuration":
            return [self._setting(item.get("section") or "") for item in params.get("items") or []]
        if method == "workspace/workspaceFolders":
            return [{"uri": self.root.as_uri(), "name": self.root.name}]
        return None

    def _setting(self, section: str):
        """Settings pyright asks for. A project's own config file still wins over these."""
        python = python_for(self.root)
        # Without the project's interpreter every third-party import looks missing: hide that.
        quiet = {} if python else {"reportMissingImports": "none", "reportMissingModuleSource": "none"}
        analysis = {"diagnosticSeverityOverrides": quiet, "typeCheckingMode": "standard"}
        if section == "python":
            return {"pythonPath": python} if python else {}
        if section in ("python.analysis", "basedpyright.analysis"):
            return analysis
        if section in ("basedpyright", "pyright"):
            return {"analysis": analysis}
        return None

    def request(self, method: str, params: dict, timeout: float):
        with self._write:
            self._next += 1
            rid = self._next
        q: queue.Queue = queue.Queue()
        self._replies[rid] = q
        try:
            self._send({"id": rid, "method": method, "params": params})
            return q.get(timeout=max(timeout, 0.01))
        except (queue.Empty, OSError, ValueError):
            return None
        finally:
            self._replies.pop(rid, None)

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    # --- lifecycle
    def initialize(self, timeout: float) -> bool:
        folder = {"uri": self.root.as_uri(), "name": self.root.name}
        reply = self.request("initialize", {
            "processId": os.getpid(), "rootUri": folder["uri"], "rootPath": str(self.root),
            "workspaceFolders": [folder],
            "capabilities": {
                "textDocument": {"publishDiagnostics": {"versionSupport": True}, "synchronization": {}},
                "workspace": {"configuration": True, "workspaceFolders": True},
            },
        }, timeout)
        if not reply or "result" not in reply:
            return False
        try:
            self.notify("initialized", {})
            self.notify("workspace/didChangeConfiguration", {"settings": {}})
        except (OSError, ValueError):
            return False
        return True

    def close(self) -> None:
        try:
            if self.proc.poll() is None and not self.dead:
                self.request("shutdown", {}, 0.5)
                self.notify("exit", {})
                self.proc.wait(timeout=0.5)
        except Exception:
            pass
        self.dead = True
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=1)
        except Exception:
            pass

    # --- documents
    def check(self, path: Path, text: str, timeout: float) -> list | None:
        """Send this text for `path`; the server's diagnostics for it, or None on timeout."""
        key = _key(path)
        with self._lock:
            self.used = time.monotonic()
            with self._cond:
                before = self._reports.get(key, (0, None, []))[0]
            version = self._versions.get(key, 0) + 1
            self._versions[key] = version
            try:
                if version == 1:
                    self.notify("textDocument/didOpen", {"textDocument": {
                        "uri": path.as_uri(), "languageId": self.spec.language(path), "version": version, "text": text}})
                else:
                    self.notify("textDocument/didChange", {
                        "textDocument": {"uri": path.as_uri(), "version": version},
                        "contentChanges": [{"text": text}]})
            except (OSError, ValueError):
                self.dead = True
                return None
            return self._wait(key, before, version, timeout)

    def _wait(self, key: str, before: int, version: int, timeout: float) -> list | None:
        end = time.monotonic() + timeout

        def fresh() -> bool:
            count, ver, _ = self._reports.get(key, (0, None, []))
            return count > before and (ver is None or ver >= version)

        with self._cond:
            while not fresh():
                left = end - time.monotonic()
                if left <= 0 or self.dead:
                    return None
                self._cond.wait(left)
            # some servers send a quick report and then a fuller one: keep the last within SETTLE
            seen = self._reports[key][0]
            settle = min(time.monotonic() + SETTLE, end)
            while (left := settle - time.monotonic()) > 0:
                self._cond.wait(left)
                if self._reports[key][0] != seen:
                    seen = self._reports[key][0]
                    settle = min(time.monotonic() + SETTLE, end)
            return list(self._reports[key][2])


# ---------------------------------------------------------------------------- all servers
class Manager:
    """The running servers, one per (language, project root), shared by every chat."""

    def __init__(self):
        self._lock = threading.Lock()
        self._servers: dict[tuple[str, str], Server] = {}
        self._failed: dict[tuple[str, str], int] = {}  # failed starts or crashes per server

    def server_for(self, path: Path) -> tuple[Server | None, bool]:
        """The server for this file, started if needed. Second value: it was just started."""
        spec = spec_for(path)
        if spec is None:
            return None, False
        root = project_root(path, spec)
        key = (spec.name, _key(root))
        with self._lock:
            server = self._servers.get(key)
            if server and not server.dead:
                return server, False
            if server:  # it crashed
                self._servers.pop(key, None)
                self._failed[key] = self._failed.get(key, 0) + 1
            if self._failed.get(key, 0) >= 2:
                return None, False
            command = find_command(spec, root)
            if command is None:
                return None, False
            if len(self._servers) >= MAX_SERVERS:
                oldest = min(self._servers, key=lambda k: self._servers[k].used)
                self._servers.pop(oldest).close()
            try:
                server = Server(spec, command, root)
            except OSError:
                self._failed[key] = self._failed.get(key, 0) + 1
                return None, False
            self._servers[key] = server
        if not server.initialize(FIRST_WAIT):
            with self._lock:
                if self._servers.get(key) is server:
                    self._servers.pop(key)
                self._failed[key] = self._failed.get(key, 0) + 1
            server.close()
            return None, False
        return server, True

    def running(self) -> list[Server]:
        with self._lock:
            return [s for s in self._servers.values() if not s.dead]

    def close(self) -> None:
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
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


# ---------------------------------------------------------------------------- the edit hook
def _errors(diags: list) -> list[dict]:
    return [d for d in diags if isinstance(d, dict) and d.get("severity", 1) == 1]


def _sig(d: dict) -> tuple:
    return (d.get("message", ""), str(d.get("code", "")), d.get("source", ""))


class Check:
    """Started before an edit is approved, so the server's startup and the before-picture run
    while the user reads the diff; `report` after the write gives the errors the edit added."""

    def __init__(self, path: Path, old_text: str | None):
        self.path = path
        self.server: Server | None = None
        self.fresh = False
        self.baseline: list | None = [] if old_text is None else None
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._prepare, args=(old_text,), daemon=True, name="lsp-prepare")
        self._thread.start()

    def _prepare(self, old_text: str | None) -> None:
        try:
            self.server, self.fresh = manager().server_for(self.path)
            if self.server and old_text is not None:
                self.baseline = self.server.check(self.path, old_text, FIRST_WAIT if self.fresh else WAIT)
        except Exception:
            self.server = None

    def report(self, new_text: str, shown_path: str) -> str:
        try:
            self._thread.join(max(2 * FIRST_WAIT - (time.monotonic() - self._started), 0.5))
            if self._thread.is_alive() or self.server is None or self.server.dead:
                return ""
            diags = self.server.check(self.path, new_text, FIRST_WAIT if self.fresh and self.baseline is None else WAIT)
            if diags is None:
                return ""
            before: dict[tuple, int] = {}
            for d in _errors(self.baseline or []):
                before[_sig(d)] = before.get(_sig(d), 0) + 1
            new = []
            for d in _errors(diags):
                if before.get(_sig(d), 0) > 0:
                    before[_sig(d)] -= 1
                else:
                    new.append(d)
            return format_errors(new, shown_path, self.server.label, len(_errors(diags)) - len(new))
        except Exception:
            return ""


def format_errors(errors: list[dict], shown_path: str, server: str, old: int = 0) -> str:
    if not errors:
        return ""

    def start(d: dict) -> dict:
        return (d.get("range") or {}).get("start") or {}

    errors = sorted(errors, key=lambda d: (start(d).get("line", 0), start(d).get("character", 0)))
    n = len(errors)
    text = f"\n{server} reports {n} new error{'s' if n > 1 else ''} in {shown_path}:"
    tail = f"\n  ({old} error{'s' if old > 1 else ''} already in the file before this edit not shown)" if old else ""
    budget = MAX_CHARS - len(tail) - 30  # room for the "... and N more" line
    shown = 0
    for d in errors[:MAX_LINES]:
        msg = " ".join(str(d.get("message", "")).split())
        if len(msg) > 200:
            msg = msg[:197] + "..."
        line = f"\n  {start(d).get('line', 0) + 1}:{start(d).get('character', 0) + 1} {msg}"
        if len(text) + len(line) > budget:
            break
        text += line
        shown += 1
    if n > shown:
        text += f"\n  ... and {n - shown} more"
    return text + tail


def start(path: Path, old_text: str | None) -> Check | None:
    """Called before an edit is approved. None when diagnostics are off or no server fits."""
    try:
        if spec_for(path) is None or not enabled():
            return None
        return Check(path, old_text)
    except Exception:
        return None


def status(root: Path) -> list[tuple[str, str]]:
    """(language server, what Vesper found for this folder), for /diagnostics."""
    running = {s.spec.name for s in manager().running()}
    out = []
    for spec in SPECS:
        cmd = find_command(spec, root)
        if cmd is None:
            state = "not installed"
        else:
            state = f"{'running' if spec.name in running else 'found'}: {Path(cmd[0]).name}"
        out.append((spec.name, state))
    return out
