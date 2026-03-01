#!/usr/bin/env python3
"""Mock print-my-ride — consumes stdin and writes a stub markdown file.

When --save-md <path> is supplied (as cl always does), writes a minimal
markdown response to that path so history-file content tests have something
to assert on.
"""

import sys

# Drain stdin so the upstream pipeline does not stall.
for _line in sys.stdin:
    pass

args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--save-md" and i + 1 < len(args):
        with open(args[i + 1], "w") as f:
            f.write("# Response\n\nHello from mock Claude!\n")
        break
