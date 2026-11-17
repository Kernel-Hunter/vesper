"""Vesper desktop launcher: `vesper-app`.

Modes:
  (no flag)   start the local API on a free 127.0.0.1 port and open the UI in
              Chrome/Edge/Brave app mode (looks like a normal window), or in the
              default browser when none of those is installed.
  --browser   same as no flag (kept for older shortcuts).
  --engine    for the Electron shell: start the API, print one handshake line on
              stdout, and exit as soon as stdin closes (parent quit or crashed).
  --serve     API only, for development; prints the URL to open.
  --smoke     prove a packaged build starts, using a throwaway home folder.

Handshake printed by --engine once the server is listening (one line, flushed):

    VESPER_READY {"port": 51234, "token": "..."}

VESPER_PORT fixes the port and VESPER_TOKEN fixes the token in --engine and --serve.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

TITLE = "Vesper"
READY_PREFIX = "VESPER_READY"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port() -> int:
    return int(os.getenv("VESPER_PORT") or _free_port())


def _token() -> str:
    return os.getenv("VESPER_TOKEN") or secrets.token_urlsafe(24)


def allowed_hosts(port: int) -> list[str]:
    return [f"127.0.0.1:{port}", f"localhost:{port}"]


def _make_app(token: str, port: int):
    from vesper.server import create_app

    return create_app(token, allowed_hosts=allowed_hosts(port))


def start_server(token: str, port: int | None = None):
    """Starts uvicorn in a daemon thread and returns (app, port, thread) once it listens."""
    import uvicorn

    port = port or _free_port()
    app = _make_app(token, port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"local server did not start on 127.0.0.1:{port}")
    return app, port, thread


def _browser_candidates() -> list[str]:
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
             "msedge", "brave-browser"]
    found = [exe for exe in (shutil.which(n) for n in names) if exe]
    if sys.platform == "darwin":
        for app, binary in (("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge"),
                            ("Brave Browser", "Brave Browser"), ("Chromium", "Chromium")):
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                path = os.path.join(root, f"{app}.app", "Contents", "MacOS", binary)
                if os.path.exists(path):
                    found.append(path)
    elif sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if not root:
                continue
            for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                        r"BraveSoftware\Brave-Browser\Application\brave.exe"):
                path = os.path.join(root, rel)
                if os.path.exists(path):
                    found.append(path)
    return found


def _app_mode_browser(url: str) -> bool:
    """Chrome, Chromium, Edge or Brave in --app mode looks like a normal window. Blocks until it closes."""
    from vesper.providers import HOME

    for exe in _browser_candidates():
        try:
            proc = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={HOME / 'browser-profile'}",
                                     "--window-size=1240,820"])
        except OSError:
            continue
        proc.wait()
        return True
    return False


def _exit_when_stdin_closes(before_exit=None):
    """Electron keeps our stdin open. EOF means the parent is gone, so we go too.
    `before_exit` runs first (it stops running answers so they are saved)."""
    stdin = sys.stdin
    if stdin is None:  # pythonw / GUI launch without pipes: nothing to watch
        return
    try:
        fd = stdin.fileno()
    except (OSError, ValueError):
        return

    def watch():
        try:
            while os.read(fd, 4096):
                pass
        except OSError:
            pass
        if before_exit is not None:
            try:
                before_exit()
            except Exception:  # noqa: BLE001 - quitting must not hang on a failed save
                pass
        os._exit(0)

    threading.Thread(target=watch, name="stdin-watch", daemon=True).start()


def engine_mode() -> int:
    """`--engine`: child process of the Electron shell."""
    token = _token()
    app, port, thread = start_server(token, _port())
    sys.stdout.write(f"{READY_PREFIX} {json.dumps({'port': port, 'token': token})}\n")
    sys.stdout.flush()
    # Electron gives the engine 3 seconds after closing stdin; running answers get 2 of them to save.
    _exit_when_stdin_closes(lambda: app.state.engine.shutdown(timeout=2.0))
    thread.join()  # the server only stops when the process is killed
    return 0


def smoke_test() -> int:
    """`--smoke`: prove a packaged build starts. Uses a throwaway home folder."""
    import urllib.request

    home = tempfile.mkdtemp(prefix="vesper-smoke-")
    os.environ["VESPER_HOME"] = home  # before vesper.providers is imported: it reads this once

    from vesper import providers as prov

    for pid in prov.PROVIDERS:  # every client must construct
        prov.get_llm(pid, "smoke-model")
    token = secrets.token_urlsafe(16)
    app, port, _ = start_server(token)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-Token": token})
    state = json.load(urllib.request.urlopen(req, timeout=10))
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read()
    import importlib

    for name in ("mcp", "commands", "documents", "webtools", "attachments"):  # loaded lazily: prove they are packaged
        importlib.import_module(f"vesper.{name}")
    extras = {}
    for path in ("/api/mcp", "/api/commands"):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Token": token})
        extras[path] = json.load(urllib.request.urlopen(r, timeout=10))
    ok = (bool(state.get("providers")) and b"<html" in page.lower()
          and "servers" in extras["/api/mcp"] and "commands" in extras["/api/commands"])
    print("smoke", "ok" if ok else f"failed: {sorted(state)}")
    return 0 if ok else 1


def serve_only():
    """`--serve`: no window, for development. VESPER_PORT / VESPER_TOKEN fix the address."""
    import uvicorn

    token = _token()
    port = _port()
    app = _make_app(token, port)
    print(f"Open http://127.0.0.1:{port}/#token={token}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    if "--smoke" in sys.argv:
        sys.exit(smoke_test())
    if "--engine" in sys.argv:
        sys.exit(engine_mode())
    if "--serve" in sys.argv:
        return serve_only()

    token = secrets.token_urlsafe(24)
    _, port, _ = start_server(token)
    url = f"http://127.0.0.1:{port}/#token={token}"

    if not _app_mode_browser(url):
        webbrowser.open(url)
        print(f"{TITLE} is running at {url}\nClose this window or press Ctrl+C to quit.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
