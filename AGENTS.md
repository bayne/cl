# AGENTS.md — cl

## Project overview

`cl` is a single-file CLI tool (`cl_tool.py`) that wraps the `claude` CLI with:
- Vim-based prompt editing (split view when continuing a session)
- Piped-stdin wrapping (pre-populates the editor with a fenced code block)
- Stream-json output piped through `print-my-ride` for rich rendering
- Session history saved to `~/.local/cl/sessions/<date>/<slug>.md` with YAML front matter
- Semantic slug generation: calls `claude --print` to produce a 2-4 word kebab-case filename for each new session
- Project-specific continue (`-c`): scans session front matter to resume the most recent session for the current project
- Session resume (`-r`): interactive picker across all sessions

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
| `_parse_front_matter(path)` | Parses YAML front matter (`session`, `project`, `date`) from a session file |
| `find_sessions(hist_dir)` | Returns `.md` files sorted newest-first by mtime |
| `find_session_file(base, session_id)` | Finds an existing session file by `session:` field in front matter |
| `find_project_sessions(base, project_dir)` | Returns session files whose `project:` matches *project_dir*, newest-first |
| `select_session_fzf(sessions)` | Interactive fzf picker (returns None if fzf unavailable) |
| `select_session_menu(sessions)` | Numbered-menu fallback; accepts `_input_fn` for testing |
| `select_session(sessions)` | Tries fzf, falls back to menu |
| `read_piped_stdin()` | Returns stdin content when not a tty, else None |
| `build_stdin_block(content)` | Wraps content in a Markdown fenced code block |
| `_in_tmux()` | Returns True when `$TMUX` is set (running inside tmux) |
| `_edit_prompt_tmux(ed, tmpfile, hist_file)` | Opens editor in a tmux split pane; shows history in a companion pane |
| `edit_prompt(initial, hist_file)` | Opens `$EDITOR`; dispatches to tmux path or vim split-view fallback |
| `extract_session_id(json_path)` | Returns first `session_id` from newline-delimited JSON |
| `_sanitize_slug(raw)` | Converts raw text to a kebab-case filename slug (max 60 chars, 5 words) |
| `generate_slug(prompt, project_dir)` | Calls `claude --print` to produce a semantic slug; falls back to timestamp |
| `run_pipeline(prompt, args, ...)` | Runs `claude | tee | print-my-ride` pipeline with threading |
| `append_to_history(hist_file, ...)` | Appends `> prompt\n\nmd_content` to the session file; writes front matter on creation |
| `parse_args(argv)` | Splits cl-specific flags from claude pass-throughs |
| `main(argv)` | Entry point — orchestrates the full flow |

## Key conventions

- **Single-file**: do not split `cl_tool.py` into multiple files.
- **No Python dependencies**: `cl` uses only the stdlib.  All I/O is via
  `subprocess` (for external tools) or `pathlib`/`open()` (for file ops).
- **Avoid shelling out**: git-config reading uses `configparser` directly.
  The only subprocess calls are `git config --global` (one write), `$EDITOR`,
  `claude` (pipeline and slug generation), `fzf` (optional), `print-my-ride`,
  and `tmux` (when inside tmux).
- **Session file naming**: new sessions are named by a semantic slug generated
  via `claude --print`.  The Claude session ID is stored in YAML front matter
  (`session:` field), not in the filename.  `find_session_file()` searches
  front matter to locate existing sessions.
- **Project-specific continue**: `cl -c` scans session front matter for
  `project:` matching `Path.cwd()` and uses `--resume <id>` instead of
  `--continue`.  Falls back to `--continue` when no project session exists.
- **Threading**: `run_pipeline` uses a background thread for the tee so
  neither `claude` nor `print-my-ride` blocks on a full pipe buffer.
- **Testability**: functions that need tty I/O accept `_input_fn` or `tty`
  parameters so tests can inject fakes without monkey-patching `open`.
- **Tmux-aware editing**: when `$TMUX` is set, `edit_prompt()` delegates to
  `_edit_prompt_tmux()` which opens the editor in a `tmux split-window` pane
  and blocks with `tmux wait-for`.  For continue/resume sessions, history is
  shown in a separate tmux pane (`less +G`) instead of a vim internal split.
  When not in tmux, the original vim `-o` split behaviour is preserved.

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
| `mock_claude.py` | Emits fixed stream-json (when `--output-format=stream-json` present) or plain slug text; logs argv to stderr |
| `mock_editor.py` | Writes `MOCK_EDITOR_PROMPT` env var to the target file (no tty needed) |
| `mock_pmr.py` | Drains stdin; writes stub markdown to `--save-md` path |

## Argument handling

| Flag | Stored locally | Forwarded to claude |
|---|---|---|
| `-c` / `--continue` | `ns.continue_session = True` | No — `main()` adds `--resume <id>` (project session found) or `--continue` (fallback) |
| `-r` / `--resume` | `ns.resume_session = True` | No (replaced by `--resume <id>`) |
| Everything else | — | Yes (verbatim) |

## Session file format

Session files live under `~/.local/cl/sessions/<YYYY-MM-DD>/<slug>.md`.
Each file starts with YAML front matter:

```yaml
---
session: <claude-session-id>
project: /absolute/path/to/working/directory
date: YYYY-MM-DD
---
```

- **`session`** — the Claude API session ID; used by `find_session_file()` and
  for `--resume <id>` when continuing.
- **`project`** — the working directory when the session was started; used by
  `find_project_sessions()` to scope `cl -c` to the current project.
- The filename slug is generated by `generate_slug()` which calls
  `claude --print` with a prompt asking for a 2-4 word kebab-case summary.
  On failure it falls back to `session-HHMMSS`.

## What NOT to do

- Do not split `cl_tool.py` into multiple files.
- Do not add Python package dependencies — stdlib only.
- Do not use `subprocess` for reading git config — use `configparser`.
- Do not bypass the `_input_fn` parameter pattern when adding new tty-reading
  code; it is the injection point for unit tests.
- Do not hard-code `vim` as the editor — always respect `$EDITOR`.
