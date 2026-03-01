#!/usr/bin/env python3
"""cl — interactive Claude CLI with session history and resume.

Usage:
  cl [OPTIONS] [CLAUDE_ARGS...]

Options:
  -c, --continue   Continue the most recent session (also forwarded to claude)
  -r, --resume     Interactively pick a past session to resume

All other options are forwarded verbatim to the `claude` command.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path


# ── Constants ─────────────────────────────────────────────────────────────────

_SESSIONS_DIR = Path.home() / ".local" / "cl" / "sessions"

_CLAUDE_FIXED_ARGS = ["--print", "--verbose", "--output-format=stream-json"]
_PMR_FIXED_ARGS = ["-l", "full", "-m", "full"]
_GIT_EXCLUDE_ENTRY = ".cl_history/"


# ── Git excludes ──────────────────────────────────────────────────────────────


def _read_global_excludes_file() -> Path | None:
    """Return the global gitconfig core.excludesFile path, or None if unset.

    Reads ~/.gitconfig directly with configparser — no subprocess.
    """
    gitconfig = Path.home() / ".gitconfig"
    if not gitconfig.exists():
        return None
    cfg = configparser.RawConfigParser()
    try:
        cfg.read(gitconfig)
        value = cfg.get("core", "excludesFile")
        return Path(os.path.expandvars(value)).expanduser()
    except (configparser.NoSectionError, configparser.NoOptionError, OSError):
        return None


def setup_git_excludes() -> None:
    """Ensure .cl_history/ is listed in the global git excludes file.

    - Reads core.excludesFile from ~/.gitconfig via configparser (no shell).
    - If the setting is absent, creates ~/.gitignore_global and registers it
      with a single `git config --global` subprocess call.
    - All file-content I/O (checking / appending the entry) uses pathlib /
      open() — no shell redirection.
    """
    excludes_path = _read_global_excludes_file()
    if excludes_path is None:
        excludes_path = Path.home() / ".gitignore_global"
        # One subprocess call: write the path into gitconfig safely.
        subprocess.run(
            [
                "git",
                "config",
                "--global",
                "core.excludesFile",
                str(excludes_path),
            ],
            capture_output=True,
            check=False,
        )

    if excludes_path.exists():
        lines = excludes_path.read_text().splitlines()
        if _GIT_EXCLUDE_ENTRY in lines:
            return
        with open(excludes_path, "a") as f:
            f.write(_GIT_EXCLUDE_ENTRY + "\n")
    else:
        excludes_path.write_text(_GIT_EXCLUDE_ENTRY + "\n")


# ── Session management ────────────────────────────────────────────────────────


def find_sessions(sessions_base: Path) -> list[Path]:
    """Return .md files under sessions_base (across all date subdirs) sorted newest-first."""
    return sorted(
        sessions_base.glob("*/*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def find_session_file(sessions_base: Path, session_id: str) -> Path | None:
    """Find an existing session file by ID across all date subdirs, or None."""
    for p in sessions_base.glob(f"*/{session_id}.md"):
        return p
    return None


def select_session_fzf(sessions: list[Path]) -> Path | None:
    """Pick a session interactively with fzf.

    Returns None if fzf is unavailable, the user cancels, or no selection
    is made.  fzf opens /dev/tty internally for its TUI; session list is
    supplied via stdin pipe.
    """
    if not sessions or not shutil.which("fzf"):
        return None
    session_strs = "\n".join(str(s) for s in sessions)
    result = subprocess.run(
        [
            "fzf",
            "--prompt=Resume session: ",
            "--preview=tail -40 {}",
            "--preview-window=up:40%:wrap",
            "--no-sort",
        ],
        input=session_strs,
        stdout=subprocess.PIPE,
        text=True,
    )
    selected = result.stdout.strip()
    return Path(selected) if selected else None


def select_session_menu(
    sessions: list[Path],
    *,
    _input_fn=None,
) -> Path | None:
    """Numbered-menu session picker (fallback when fzf is unavailable).

    *_input_fn* is an optional callable that returns a line of text; used
    in tests to avoid opening /dev/tty.
    """
    if not sessions:
        return None
    print("Select a session to resume:", file=sys.stderr)
    for i, s in enumerate(sessions, 1):
        print(f"  {i}) {s.parent.name}/{s.name}", file=sys.stderr)
    sys.stderr.write("Enter number: ")
    sys.stderr.flush()

    try:
        if _input_fn is not None:
            choice = _input_fn()
        else:
            with open("/dev/tty") as tty:
                choice = tty.readline().strip()
    except OSError:
        return None

    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
    return None


def select_session(sessions: list[Path]) -> Path | None:
    """Try fzf first; fall back to the numbered menu."""
    if not sessions:
        return None
    result = select_session_fzf(sessions)
    if result is None:
        result = select_session_menu(sessions)
    return result


# ── Prompt handling ───────────────────────────────────────────────────────────


def read_piped_stdin() -> str | None:
    """Read and return all of stdin when it is a pipe.

    Returns None if stdin is an interactive tty or if the pipe is empty.
    """
    if sys.stdin.isatty():
        return None
    content = sys.stdin.read()
    return content if content else None


def build_stdin_block(content: str) -> str:
    """Wrap *content* in a Markdown fenced code block for use as prompt context."""
    return f"\n\n```\n{content}\n```\n"


def edit_prompt(
    initial_content: str = "",
    hist_file: Path | None = None,
    *,
    editor: str | None = None,
    tty: str = "/dev/tty",
) -> str:
    """Open $EDITOR so the user can compose a prompt.

    When *hist_file* is supplied and the editor is vim/nvim, opens a split
    window (history read-only on top, prompt editable on bottom).

    Returns the full text of the edited file (may be empty / whitespace-only).
    """
    ed = editor or os.environ.get("EDITOR", "vim")
    fd, tmp = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(initial_content)
        tmpfile = Path(tmp)

        # Open /dev/tty so the editor gets keyboard input even when our stdin
        # is a pipe.
        tty_stdin = None
        try:
            if os.path.exists(tty):
                tty_stdin = open(tty)  # noqa: SIM115 — context mgr below
        except OSError:
            pass

        try:
            if hist_file and hist_file.exists() and ed in ("vim", "nvim"):
                subprocess.run(
                    [
                        ed,
                        "-o",
                        str(hist_file),
                        str(tmpfile),
                        "-c",
                        "wincmd k | setlocal readonly nomodifiable | normal G",
                        "-c",
                        "wincmd j",
                    ],
                    stdin=tty_stdin,
                )
            else:
                subprocess.run([ed, str(tmpfile)], stdin=tty_stdin)
        finally:
            if tty_stdin:
                tty_stdin.close()

        return tmpfile.read_text()
    finally:
        Path(tmp).unlink(missing_ok=True)


# ── Session ID extraction ─────────────────────────────────────────────────────


def extract_session_id(json_path: Path) -> str | None:
    """Return the first session_id value found in newline-delimited JSON.

    Each line of *json_path* is parsed independently; malformed lines are
    skipped.  Returns None if the file is absent, empty, or contains no
    session_id.
    """
    if not json_path.exists():
        return None
    for line in json_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = data.get("session_id")
        if sid:
            return sid
    return None


# ── Pipeline ──────────────────────────────────────────────────────────────────


def run_pipeline(
    prompt: str,
    claude_args: list[str],
    json_path: Path,
    md_path: Path,
    resume_id: str | None = None,
    *,
    claude_bin: str = "claude",
    pmr_bin: str = "print-my-ride",
) -> int:
    """Run the pipeline: echo prompt | claude [...] | tee json_path | pmr [...].

    Implements the tee in Python using a background thread so neither process
    blocks on a full pipe buffer.  Returns the exit code of print-my-ride.

    Parameters
    ----------
    prompt:       The user's typed prompt (sent to claude's stdin).
    claude_args:  Extra flags forwarded to claude (e.g. --continue).
    json_path:    Where to tee claude's raw stream-json output.
    md_path:      Path passed to print-my-ride --save-md.
    resume_id:    Session ID to resume (adds --resume <id> to claude invocation).
    claude_bin:   Override the claude binary name/path (useful in tests).
    pmr_bin:      Override the print-my-ride binary name/path (useful in tests).
    """
    claude_cmd = [claude_bin] + _CLAUDE_FIXED_ARGS + claude_args
    if resume_id:
        claude_cmd += ["--resume", resume_id]

    pmr_cmd = [pmr_bin] + _PMR_FIXED_ARGS + ["--save-md", str(md_path)]

    with (
        subprocess.Popen(
            claude_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
        ) as claude_proc,
        subprocess.Popen(
            pmr_cmd,
            stdin=subprocess.PIPE,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
        ) as pmr_proc,
    ):
        assert claude_proc.stdin is not None
        assert claude_proc.stdout is not None
        assert pmr_proc.stdin is not None

        # Send prompt and close claude's stdin.
        claude_proc.stdin.write(prompt + "\n")
        claude_proc.stdin.close()

        # Background thread reads claude's output and fans it out to both the
        # JSON file and pmr's stdin.  Threading ensures claude's stdout buffer
        # never fills while pmr processes slowly.
        def _tee() -> None:
            with open(json_path, "w") as jf:
                for line in claude_proc.stdout:
                    jf.write(line)
                    pmr_proc.stdin.write(line)  # type: ignore[union-attr]
                    pmr_proc.stdin.flush()  # type: ignore[union-attr]
            pmr_proc.stdin.close()  # type: ignore[union-attr]

        tee_thread = threading.Thread(target=_tee, daemon=True)
        tee_thread.start()
        tee_thread.join()

    return pmr_proc.returncode


# ── History ───────────────────────────────────────────────────────────────────


def append_to_history(
    hist_file: Path,
    prompt: str,
    md_file: Path,
    *,
    project_dir: Path | None = None,
) -> None:
    """Append "> {prompt}\\n\\n{md_content}" to *hist_file*.

    Creates the file if it does not yet exist; appends if it does (supporting
    multi-turn sessions under a single file).  On creation, writes a YAML
    front-matter block with session ID, project directory, and date when
    *project_dir* is supplied.
    """
    is_new = not hist_file.exists()
    md_content = md_file.read_text() if md_file.exists() else ""
    with open(hist_file, "a") as f:
        if is_new and project_dir is not None:
            f.write("---\n")
            f.write(f"session: {hist_file.stem}\n")
            f.write(f"project: {project_dir}\n")
            f.write(f"date: {date.today().isoformat()}\n")
            f.write("---\n\n")
        f.write(f"> {prompt}\n\n")
        f.write(md_content)


# ── Argument parsing ──────────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse cl-specific flags; return (namespace, args_to_forward_to_claude).

    - ``-c`` / ``--continue`` → stored in namespace AND appended to the
      claude passthrough list as ``--continue``.
    - ``-r`` / ``--resume``   → stored in namespace only; NOT forwarded
      (replaced later by ``--resume <session_id>``).
    - Everything else         → forwarded as-is via parse_known_args.
    """
    parser = argparse.ArgumentParser(
        prog="cl",
        description="Interactive Claude CLI with session history.",
        add_help=True,
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the most recent Claude session (also forwarded to claude).",
    )
    parser.add_argument(
        "-r",
        "--resume",
        dest="resume_session",
        action="store_true",
        help="Interactively pick a past session to resume.",
    )
    ns, extra = parser.parse_known_args(argv)

    # --continue is also forwarded to claude so it continues server-side.
    claude_passthrough: list[str] = list(extra)
    if ns.continue_session:
        claude_passthrough.append("--continue")

    return ns, claude_passthrough


# ── Main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Entry point.  Returns an exit code (0 = success)."""
    if argv is None:
        argv = sys.argv[1:]

    ns, claude_passthrough = parse_args(argv)

    sessions_base = Path.home() / ".local" / "cl" / "sessions"
    today_dir = sessions_base / date.today().isoformat()
    today_dir.mkdir(parents=True, exist_ok=True)

    # Consume piped stdin *before* we hand control to the editor (which needs
    # the terminal for keyboard input).
    piped = read_piped_stdin()
    initial_content = build_stdin_block(piped) if piped and piped.strip() else ""

    # ── Resume session selection ───────────────────────────────────────────────
    resume_id: str | None = None
    session_file: Path | None = None

    if ns.resume_session:
        sessions = find_sessions(sessions_base)
        if not sessions:
            print(f"No sessions found in {sessions_base}", file=sys.stderr)
            return 1
        session_file = select_session(sessions)
        if session_file is None:
            return 0
        resume_id = session_file.stem  # filename sans .md = session ID
        ns.continue_session = True  # open split-view just like -c

    # ── Find history file for split-view ──────────────────────────────────────
    hist_file: Path | None = None
    if ns.continue_session:
        hist_file = session_file or next(iter(find_sessions(sessions_base)), None)

    # ── Edit prompt ───────────────────────────────────────────────────────────
    prompt = edit_prompt(initial_content, hist_file)
    if not prompt.strip():
        return 0

    # Echo prompt so it is visible in the terminal output.
    print(prompt)

    # ── Run pipeline ──────────────────────────────────────────────────────────
    with (
        tempfile.NamedTemporaryFile(suffix=".json", delete=False) as _tj,
        tempfile.NamedTemporaryFile(suffix=".md", delete=False) as _tm,
    ):
        tmpjson = Path(_tj.name)
        tmpmd = Path(_tm.name)

    try:
        run_pipeline(prompt, claude_passthrough, tmpjson, tmpmd, resume_id)

        session_id = extract_session_id(tmpjson)
        if session_id:
            # Append to an existing file for this session if present (e.g. cross-day
            # continuation), otherwise start a new file in today's date dir.
            target = find_session_file(sessions_base, session_id) or (
                today_dir / f"{session_id}.md"
            )
            append_to_history(target, prompt, tmpmd, project_dir=Path.cwd())
    finally:
        tmpjson.unlink(missing_ok=True)
        tmpmd.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
