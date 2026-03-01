# cl

Interactive CLI wrapper for [Claude](https://claude.ai) with session history,
prompt editing, and rich output via
[print-my-ride](https://github.com/bpayne/print-my-ride).

## What it does

1. Opens `$EDITOR` (default: `vim`) so you can compose a prompt.
2. Pipes the prompt through `claude --print --verbose --output-format=stream-json`.
3. Renders the stream-json output with `print-my-ride -l full -m full`.
4. Saves the conversation (prompt + rendered response) to
   `.cl_history/<session_id>.md` in the current directory.
5. Supports continuing or resuming past sessions.

## Requirements

- Python ≥ 3.14
- [`claude` CLI](https://claude.ai/download) in PATH
- [`print-my-ride`](../README.md) in PATH (install from the parent project)
- `fzf` (optional — used for the session picker; falls back to a numbered menu)

## Installation

```bash
# From the cl/ directory
uv pip install -e .

# Or install alongside print-my-ride from the repo root
uv pip install -e . -e cl/
```

## Usage

```bash
cl                   # Open editor, send prompt, save history
cl -c                # Continue the most recent session
cl -r                # Pick a past session to resume (fzf or numbered menu)
cat file.py | cl     # Pipe content; it is wrapped in a code fence for context

# Extra flags are forwarded to claude:
cl --model claude-3-5-sonnet-20241022
cl -c --verbose
```

## Session history

Each session is saved as `.cl_history/<session_id>.md` in the directory where
you run `cl`.  Add `.cl_history/` to your global git excludes — `cl` does this
automatically on first run.

Format of each history entry:
```
> Your prompt here

## Response heading

Response content rendered by print-my-ride…
```

Continuing (`-c`) or resuming (`-r`) a session appends new turns to the same
file so the full conversation is preserved.

## Flags

| Flag | Description |
|---|---|
| `-c`, `--continue` | Continue the most recent Claude session (forwarded to claude) |
| `-r`, `--resume` | Interactively pick a past session to resume |

All other flags (e.g. `--model`, `--verbose`) are forwarded to `claude`.

## Editor split view

When continuing (`-c`) or resuming (`-r`) a session, `cl` opens vim with a
horizontal split: the previous conversation is shown read-only on top, and
your new prompt is editable on the bottom.

This only works when `$EDITOR` is `vim` or `nvim`.  Other editors open a plain
single-file view.

## Piped stdin

When stdin is a pipe, `cl` wraps the input in a fenced Markdown code block and
pre-populates the editor with it:

```bash
git diff HEAD | cl      # Diff appears in editor, ready to prompt about
cat error.log | cl -c   # Append error context to ongoing session
```

## Development

```bash
cd cl/
uv run pytest            # All tests
uv run pytest -v         # Verbose
uv run pytest tests/test_unit.py   # Unit tests only
uv run pytest tests/test_e2e.py    # E2E tests only
```
