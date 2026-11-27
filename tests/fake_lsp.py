"""A tiny language server for the diagnostics tests.

Every line containing ERROR is an error, every line containing WARN a warning. Like pyright,
it asks the client for its configuration first and reports nothing until it gets an answer.
FAKE_LSP_MODE=silent: never reports. FAKE_LSP_MODE=crash: exits on the first document.
FAKE_LSP_MODE=slow-first: the first report comes 1.5 s late. Opening a document that is
already open is a client bug: the server exits, as a strict server might.
"""
import json
import time
import os
import sys

MODE = os.environ.get("FAKE_LSP_MODE", "")
inp, out = sys.stdin.buffer, sys.stdout.buffer
configured = False
waiting: list = []  # documents that arrived before the configuration answer
open_docs: set = set()
reported = 0


def send(msg):
    body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def read():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(inp.read(length))


def publish(uri, version, text):
    global reported
    if MODE == "silent":
        return
    if MODE == "slow-first" and not reported:
        time.sleep(1.5)
    reported += 1
    diags = []
    for n, line in enumerate(text.splitlines()):
        for word, severity in (("ERROR", 1), ("WARN", 2)):
            col = line.find(word)
            if col >= 0:
                diags.append({"range": {"start": {"line": n, "character": col}, "end": {"line": n, "character": col + 5}},
                              "severity": severity, "message": f"{word.lower()}: {line.strip()}", "source": "fake"})
    send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "version": version, "diagnostics": diags}})


while True:
    msg = read()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send({"id": "cfg", "method": "workspace/configuration", "params": {"items": [{"section": "python"}]}})
        send({"id": msg["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
    elif msg.get("id") == "cfg" and "result" in msg:
        configured = True
        for doc in waiting:
            publish(*doc)
        waiting.clear()
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        if MODE == "crash":
            sys.exit(1)
        p = msg["params"]
        uri = p["textDocument"]["uri"]
        if method.endswith("didOpen"):
            if uri in open_docs:
                sys.exit(2)
            open_docs.add(uri)
        text = p["textDocument"]["text"] if method.endswith("didOpen") else p["contentChanges"][-1]["text"]
        doc = (p["textDocument"]["uri"], p["textDocument"]["version"], text)
        if configured:
            publish(*doc)
        else:
            waiting.append(doc)
    elif method == "textDocument/didClose":
        open_docs.discard(msg["params"]["textDocument"]["uri"])
    elif method == "shutdown":
        send({"id": msg["id"], "result": None})
    elif method == "exit":
        break
