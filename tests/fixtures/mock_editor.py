#!/usr/bin/env python3
"""Mock editor — writes MOCK_EDITOR_PROMPT to the target file.

Understands vim-style invocations:
  editor <file>
  editor -o <hist_file> <tmpfile> -c <cmd> -c <cmd>

The target file is the last positional argument (not preceded by a flag
that takes a value, specifically -c).  The editor content is read from the
MOCK_EDITOR_PROMPT environment variable (default: "What is 2+2?").
"""

import os
import sys

files: list[str] = []
skip_next = False
for arg in sys.argv[1:]:
    if skip_next:
        skip_next = False
        continue
    if arg == "-c":
        # -c takes a vim command string as its next token — skip both.
        skip_next = True
        continue
    if arg.startswith("-"):
        # Flags without values (e.g. -o, -O, -R) — skip the flag itself.
        continue
    files.append(arg)

target = files[-1] if files else None
prompt = os.environ.get("MOCK_EDITOR_PROMPT", "What is 2+2?")

if target:
    with open(target, "w") as f:
        f.write(prompt)
