#!/usr/bin/env python3
"""Mock claude CLI — emits predictable stream-json for testing.

Behaviour:
  - Default session_id is SESSION_ID below.
  - If --resume <id> is present in argv, uses <id> as the session_id so
    resume tests can verify the correct ID is forwarded.
  - Reads (and discards) stdin so the pipeline doesn't stall.
  - Writes all args received to stderr prefixed with MOCK_CLAUDE_ARGS: so
    tests can inspect what was forwarded.
"""

import json
import sys

SESSION_ID = "sess-mock-deadbeef1234"

# Honour --resume <id>
args = sys.argv[1:]
if "--resume" in args:
    idx = args.index("--resume")
    if idx + 1 < len(args):
        SESSION_ID = args[idx + 1]

# Drain stdin so the caller's pipeline doesn't block.
sys.stdin.read()

# Announce args for test assertions.
sys.stderr.write(f"MOCK_CLAUDE_ARGS: {args}\n")
sys.stderr.flush()

# Non-stream call (e.g. slug generation) — return a plain-text slug.
if "--output-format=stream-json" not in args:
    print("mock-session-slug", flush=True)
    sys.exit(0)

MESSAGES = [
    {"type": "system", "subtype": "init", "session_id": SESSION_ID, "tools": []},
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": "Hello from mock Claude!"}],
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "is_error": False,
        "num_turns": 1,
        "session_id": SESSION_ID,
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "result": None,
        "structured_output": None,
    },
]

for msg in MESSAGES:
    print(json.dumps(msg), flush=True)
