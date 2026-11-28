"""A stand-in for a user's hook command in the hooks tests.

python hook_script.py <mode> <log file>
Every call appends the JSON it got on stdin to the log file, then:
block: stderr "no writes here", exit 2 | context: stdout "Today is release day", exit 0
fail: exit 1 | sleep: sleeps 30 s | once: exit 2 the first time, exit 0 after | ok: exit 0
"""
import json
import sys
import time
from pathlib import Path

mode, log = sys.argv[1], Path(sys.argv[2])
payload = json.loads(sys.stdin.read() or "{}")
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(payload) + "\n")
calls = len(log.read_text(encoding="utf-8").splitlines())

if mode == "block":
    print("no writes here", file=sys.stderr)
    sys.exit(2)
if mode == "context":
    print("Today is release day")
elif mode == "fail":
    print("something broke", file=sys.stderr)
    sys.exit(1)
elif mode == "sleep":
    time.sleep(30)
elif mode == "once" and calls == 1:
    print("run the tests first", file=sys.stderr)
    sys.exit(2)
