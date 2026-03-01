#!/usr/bin/env python3
"""Generate demo.cast for cl — an asciinema v2 recording.

Simulates two scenarios:
  1. Fresh `cl` — editor opens, user enters prompt, claude responds with
     tool use and a markdown answer.
  2. `cl -c` — continues the session with a follow-up question.

The cast is built programmatically (no asciinema rec required).
`print-my-ride` is invoked for real so the rendered output is authentic.

Usage:
    python demo/gen_demo.py           # writes demo/demo.cast
    asciinema play demo/demo.cast    # play it back
"""

import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
DEMO_DIR    = Path(__file__).parent

WIDTH  = 100
HEIGHT = 30


# ── Stream-json message fixtures ──────────────────────────────────────────────


def _result(
    session_id: str = "sess-cl-demo-abc1234",
    cost: float = 0.0034,
    turns: int = 2,
    ms: int = 2800,
    model: str = "claude-opus-4-6",
) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": ms,
        "duration_api_ms": int(ms * 0.82),
        "is_error": False,
        "num_turns": turns,
        "session_id": session_id,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 380,
            "output_tokens": 145,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "result": None,
        "structured_output": None,
    }


# Turn 1 — "How does cl save session history?"
TURN1 = [
    {
        "type": "system",
        "subtype": "init",
        "session_id": "sess-cl-demo-abc1234",
        "tools": [
            {"name": "Bash", "description": "Run bash commands", "input_schema": {}},
        ],
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "thinking",
                    "thinking": (
                        "The user wants to know how cl saves session history. "
                        "I'll check the sessions directory to show a real example."
                    ),
                    "signature": "sig_cl_demo_0001",
                },
                {
                    "type": "tool_use",
                    "id": "toolu_cl_demo_0001",
                    "name": "Bash",
                    "input": {
                        "command": "ls ~/.local/cl/sessions/ 2>/dev/null | head -5 "
                        "|| echo '(no sessions yet — this is your first run)'"
                    },
                },
            ],
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_cl_demo_0001",
                    "content": "2026-03-01",
                    "is_error": False,
                }
            ],
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_cl_demo_0002",
                    "name": "Bash",
                    "input": {
                        "command": "ls ~/.local/cl/sessions/2026-03-01/ | head -3"
                    },
                },
            ],
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_cl_demo_0002",
                    "content": "sess-cl-demo-abc1234.md",
                    "is_error": False,
                }
            ],
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "After each conversation `cl` writes a Markdown file to\n"
                        "`~/.local/cl/sessions/<date>/<session-id>.md`.\n\n"
                        "### What gets saved\n\n"
                        "Each file opens with YAML front-matter:\n\n"
                        "```yaml\n"
                        "---\n"
                        "session: sess-cl-demo-abc1234\n"
                        "project: /home/user/my-project\n"
                        "date: 2026-03-01\n"
                        "---\n"
                        "```\n\n"
                        "Then every turn is appended as:\n\n"
                        "```\n"
                        "> Your prompt here\n\n"
                        "Response rendered by print-my-ride…\n"
                        "```\n\n"
                        "### Resume a session\n\n"
                        "- `cl -c` continues the **most recent** session.\n"
                        "- `cl -r` opens an **fzf picker** (or numbered menu) to "
                        "select any past session.\n\n"
                        "Both modes open vim in a horizontal split: the history is "
                        "read-only on top, your new prompt editable below."
                    ),
                }
            ],
        },
    },
    _result(turns=3, ms=3200, cost=0.0041),
]


# Turn 2 — follow-up via `cl -c`
TURN2 = [
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Yes — `cl` adds `.cl_history/` (a legacy path) **and**\n"
                        "`~/.local/cl/sessions/` to your global git excludes\n"
                        "automatically on first run, so session files never appear\n"
                        "as untracked changes inside your projects.\n\n"
                        "The git excludes file (`core.excludesFile` in `~/.gitconfig`)\n"
                        "is updated with a single `git config --global` call;\n"
                        "no shell redirection is used."
                    ),
                }
            ],
        },
    },
    _result(session_id="sess-cl-demo-abc1234", turns=1, ms=1100, cost=0.0009),
]


# ── Output capture ─────────────────────────────────────────────────────────────


def capture(messages: list[dict], *, level: str = "full", meta: str = "full") -> str:
    """Run the real print-my-ride and return ANSI output."""
    input_data = "\n".join(json.dumps(m) for m in messages) + "\n"
    result = subprocess.run(
        [
            "print-my-ride",
            "--level", level,
            "--meta", meta,
            "--color-system", "256",
            "--width", str(WIDTH),
            "--divider-width", str(WIDTH),
        ],
        input=input_data,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"print-my-ride error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


# ── Cast builder ───────────────────────────────────────────────────────────────


class Cast:
    """Build an asciinema v2 cast file."""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        self.width  = width
        self.height = height
        self._events: list[list] = []
        self._t = 0.0

    def _emit(self, text: str, dt: float = 0.0) -> None:
        self._t += dt
        self._events.append([round(self._t, 6), "o", text])

    def pause(self, seconds: float) -> None:
        self._t += seconds

    def prompt(self, cwd: str = "~/my-project") -> None:
        """Emit a realistic shell prompt."""
        self._emit("\r\n")
        self._emit(
            f"\x1b[2m{cwd}\x1b[0m "         # dim cwd
            f"\x1b[1;32m❯\x1b[0m "          # bold green ❯
        )

    def type_cmd(self, cmd: str, wpm: int = 220) -> None:
        """Simulate human typing at ~wpm words-per-minute."""
        char_delay = 60.0 / (wpm * 5)
        for ch in cmd:
            extra = 0.04 if ch == " " else 0.0
            self._emit(ch, dt=char_delay + extra)
        self.pause(0.3)
        self._emit("\r\n")
        self.pause(0.12)

    def print_output(self, text: str, chunk: int = 120) -> None:
        """Emit output in chunks with small delays (mimics streaming)."""
        for i in range(0, len(text), chunk):
            self._emit(text[i : i + chunk], dt=0.012)

    def comment(self, text: str) -> None:
        """Print a dim comment line."""
        self._emit(f"\x1b[2;37m# {text}\x1b[0m\r\n")
        self.pause(0.5)

    def echo_prompt(self, text: str) -> None:
        """Show the user prompt as cl would echo it after the editor closes."""
        self._emit(f"\x1b[2m{text}\x1b[0m\r\n")
        self.pause(0.1)

    def save(self, path: Path) -> None:
        header = json.dumps({
            "version":   2,
            "width":     self.width,
            "height":    self.height,
            "timestamp": int(time.time()),
            "title":     "cl — interactive Claude CLI",
            "env":       {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
        })
        lines = [header] + [json.dumps(e) for e in self._events]
        path.write_text("\n".join(lines) + "\n")
        print(f"wrote {path}  ({len(self._events)} events, {self._t:.1f}s)")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    cast = Cast()

    # ── Scene 1: fresh `cl` ────────────────────────────────────────────────────
    cast.comment("open editor, write prompt, send to claude")
    cast.prompt()
    cast.type_cmd("cl")

    # Simulate editor opening and closing (just a brief pause + echo)
    cast._emit("\x1b[2m[vim opens — write prompt, :wq]\x1b[0m\r\n")
    cast.pause(1.2)

    # cl echoes the prompt after the editor closes
    cast.echo_prompt("How does cl save session history?")
    cast.pause(0.15)

    # Real print-my-ride output for turn 1
    cast.print_output(capture(TURN1))
    cast.pause(2.5)

    # ── Scene 2: `cl -c` continue session ─────────────────────────────────────
    cast.comment("continue the session with a follow-up")
    cast.prompt()
    cast.type_cmd("cl -c")

    cast._emit("\x1b[2m[vim split: history above, prompt below — :wq]\x1b[0m\r\n")
    cast.pause(1.0)

    cast.echo_prompt("Does cl affect my git status?")
    cast.pause(0.15)

    cast.print_output(capture(TURN2))
    cast.pause(2.5)

    out_path = DEMO_DIR / "demo.cast"
    cast.save(out_path)


if __name__ == "__main__":
    main()
