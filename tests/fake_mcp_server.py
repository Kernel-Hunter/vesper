"""A tiny MCP server over stdio for the tests: two pages of tools, a read-only one, an error."""

import json
import sys

TOOLS = [
    {"name": "echo", "description": "Say the text back.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "What to say"}},
                     "required": ["text"]}},
    {"name": "add_note", "description": "Save a note.",
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object", "properties": {}}},
]
EXTRA = int(sys.argv[1]) if len(sys.argv) > 1 else 0
TOOLS += [{"name": f"extra_{i}", "description": f"Extra tool number {i}.", "inputSchema": {"type": "object"}}
          for i in range(EXTRA)]
notes = []


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


print("this server logs to stdout before it starts", flush=True)  # must be ignored
for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None or "result" in msg or "error" in msg:
        continue  # notifications, and the client's answer to our roots/list
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})  # a request the client must answer
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params["protocolVersion"],
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        half = (len(TOOLS) + 1) // 2
        chunk = TOOLS[half:] if params.get("cursor") else TOOLS[:half]
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": chunk, **({} if params.get("cursor") else {"nextCursor": "p2"})}})
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}]}
        elif name == "add_note":
            notes.append(args.get("note"))
            result = {"content": [{"type": "text", "text": f"saved {len(notes)} note(s)"},
                                  {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="}]}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": name}]}
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}})
