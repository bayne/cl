# AGENTS.md — cl

## Project overview

`cl` is a single-file CLI tool (`cl_tool.py`) that wraps the `claude` CLI with:
- Vim-based prompt editing (split view when continuing a session)
- Piped-stdin wrapping (pre-populates the editor with a fenced code block)
- Stream-json output piped through `print-my-ride` for rich rendering
- Session history saved to `.cl_history/<session_id>.md` in the working directory
- Session resume/continue with `-r` (interactive picker) and `-c` flags

## Setup & run

```bash
# From the cl/ directory:
uv pip install -e .
cl --help

# Run tests
uv run pytest
uv run pytest tests/test_e2e.py -v
uv run pytest tests/test_unit.py -v
```

Python ≥ 3.14 is required.

## Architecture

All logic lives in `cl_tool.py`.  There are no submodules.

| Symbol | Role |
|---|---|
| `setup_git_excludes()` | Adds `.cl_history/` to the global git excludes file |
| `_read_global_excludes_file()` | Reads `core.excludesFile` from `~/.gitconfig` via `configparser` (no shell) |
| `find_sessions(hist_dir)` | Returns `.md` files sorted newest-first by mtime |
| `select_session_fzf(sessions)` | Interactive fzf picker (returns None if fzf unavailable) |
| `select_session_menu(sessions)` | Numbered-menu fallback; accepts `_input_fn` for testing |
| `select_session(sessions)` | Tries fzf, falls back to menu |
| `read_piped_stdin()` | Returns stdin content when not a tty, else None |
| `build_stdin_block(content)` | Wraps content in a Markdown fenced code block |
| `edit_prompt(initial, hist_file)` | Opens `$EDITOR`; vim split-view when hist_file supplied |
| `extract_session_id(json_path)` | Returns first `session_id` from newline-delimited JSON |
| `run_pipeline(prompt, args, ...)` | Runs `claude | tee | print-my-ride` pipeline with threading |
| `append_to_history(hist_file, ...)` | Appends `> prompt\n\nmd_content` to the session file |
| `parse_args(argv)` | Splits cl-specific flags from claude pass-throughs |
| `main(argv)` | Entry point — orchestrates the full flow |

## Key conventions

- **Single-file**: do not split `cl_tool.py` into multiple files.
- **No Python dependencies**: `cl` uses only the stdlib.  All I/O is via
  `subprocess` (for external tools) or `pathlib`/`open()` (for file ops).
- **Avoid shelling out**: git-config reading uses `configparser` directly.
  The only subprocess calls are `git config --global` (one write), `$EDITOR`,
  `claude`, `fzf` (optional), and `print-my-ride`.
- **Threading**: `run_pipeline` uses a background thread for the tee so
  neither `claude` nor `print-my-ride` blocks on a full pipe buffer.
- **Testability**: functions that need tty I/O accept `_input_fn` or `tty`
  parameters so tests can inject fakes without monkey-patching `open`.

## Testing

Tests live in `tests/`.  No pytest fixtures in `conftest.py` except the
shared `cl_env` fixture.

- `test_unit.py` — pure unit tests; no subprocesses.  Uses `monkeypatch`
  and `unittest.mock.patch` to isolate file-system and subprocess calls.
- `test_e2e.py` — subprocess tests; each test runs `cl_tool.py` via
  `sys.executable` with mock binaries for `claude`, `editor`, and
  `print-my-ride` injected via PATH and EDITOR env vars.

### Mock fixtures (`tests/fixtures/`)

| File | Purpose |
|---|---|
| `mock_claude.py` | Emits fixed stream-json; logs its argv to stderr for assertions |
| `mock_editor.py` | Writes `MOCK_EDITOR_PROMPT` env var to the target file (no tty needed) |
| `mock_pmr.py` | Drains stdin; writes stub markdown to `--save-md` path |

## Argument handling

| Flag | Stored locally | Forwarded to claude |
|---|---|---|
| `-c` / `--continue` | `ns.continue_session = True` | Yes (`--continue`) |
| `-r` / `--resume` | `ns.resume_session = True` | No (replaced by `--resume <id>`) |
| Everything else | — | Yes (verbatim) |

## What NOT to do

- Do not split `cl_tool.py` into multiple files.
- Do not add Python package dependencies — stdlib only.
- Do not use `subprocess` for reading git config — use `configparser`.
- Do not bypass the `_input_fn` parameter pattern when adding new tty-reading
  code; it is the injection point for unit tests.
- Do not hard-code `vim` as the editor — always respect `$EDITOR`.
