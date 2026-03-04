#!/usr/bin/env python3
"""Mock claude CLI that hangs forever — used to test Ctrl+C handling.

Emits the init message then sleeps indefinitely so the parent can
send SIGINT and verify graceful shutdown.
"""

import json
import signal
import sys
import time

SESSION_ID = "sess-mock-deadbeef1234"

# Drain stdin so the caller's pipeline doesn't block.
sys.stdin.read()

# Announce args for test assertions.
sys.stderr.write(f"MOCK_CLAUDE_ARGS: {sys.argv[1:]}\n")
sys.stderr.flush()

# Non-stream call (e.g. slug generation) — return a plain-text slug.
if "--output-format=stream-json" not in sys.argv[1:]:
    print("mock-session-slug", flush=True)
    sys.exit(0)

# Emit the init message so the pipeline starts.
print(json.dumps({
    "type": "system", "subtype": "init",
    "session_id": SESSION_ID, "tools": [],
}), flush=True)

# Sleep forever — the test will send SIGINT.
# Use default SIGINT handler so the process terminates on signal.
signal.signal(signal.SIGINT, signal.SIG_DFL)
while True:
    time.sleep(60)
