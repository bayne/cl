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
import re
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


def _parse_front_matter(path: Path) -> dict[str, str]:
    """Parse YAML front matter from a session file.

    Returns a dict of key-value pairs from the ``---`` delimited block at the
    top of the file.  Returns an empty dict if the file has no front matter.
    """
    try:
        text = path.read_text()
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    matter = {}
    for line in text[4:end].splitlines():
        key, _, value = line.partition(": ")
        if value:
            matter[key.strip()] = value.strip()
    return matter


def find_session_file(sessions_base: Path, session_id: str) -> Path | None:
    """Find an existing session file by session ID in front matter, or None."""
    for p in sessions_base.glob("*/*.md"):
        matter = _parse_front_matter(p)
        if matter.get("session") == session_id:
            return p
    return None


def find_project_sessions(sessions_base: Path, project_dir: Path) -> list[Path]:
    """Return session files whose ``project`` front matter matches *project_dir*.

    Results are sorted newest-first by file modification time.
    """
    results: list[Path] = []
    for p in sorted(
        sessions_base.glob("*/*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        matter = _parse_front_matter(p)
        if matter.get("project") == str(project_dir):
            results.append(p)
    return results


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


# ── Tmux helpers ─────────────────────────────────────────────────────────────


def _in_tmux() -> bool:
    """Return True if running inside a tmux session."""
    return bool(os.environ.get("TMUX"))


# ── Session summary ──────────────────────────────────────────────────────────


def _session_summary_comment(session_file: Path) -> str:
    """Build a comment block summarizing the session being resumed.

    Lines start with ``#`` and are stripped from the prompt before sending
    to Claude.
    """
    matter = _parse_front_matter(session_file)
    slug = session_file.stem
    date_str = matter.get("date", "")

    # Extract prompts from the session file.
    prompts: list[str] = []
    try:
        for line in session_file.read_text().splitlines():
            if line.startswith("> "):
                prompts.append(line[2:].strip())
    except OSError:
        pass

    header = f"# Resuming: {slug}"
    if date_str:
        header += f" ({date_str})"

    lines = [header]
    for p in prompts[-5:]:
        display = p[:80] + ("..." if len(p) > 80 else "")
        lines.append(f"#   > {display}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _strip_leading_comments(text: str) -> str:
    """Remove leading ``#`` comment lines and subsequent blank lines."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "".join(lines[i:])


def _comment_end_line(text: str) -> int:
    """Return the 1-based line number of the first non-comment, non-blank line.

    Returns 0 if the text does not start with ``#`` comment lines.
    """
    if not text or not text.lstrip().startswith("#"):
        return 0
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i + 1 if i < len(lines) else i + 1


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


def _edit_prompt_tmux(
    ed: str,
    tmpfile: Path,
    hist_file: Path | None = None,
) -> str:
    """Open editor in a tmux split pane, optionally showing history in another pane.

    Uses ``tmux split-window`` to launch the editor and ``tmux wait-for`` to
    block until the editor exits.  When *hist_file* is supplied, a separate
    tmux pane displays the history (read-only via ``less +G``) and is killed
    automatically when the editor closes.

    Returns the full text of the edited file (may be empty / whitespace-only).
    """
    channel = f"cl-edit-done-{os.getpid()}"
    hist_pane: str | None = None

    try:
        if hist_file and hist_file.exists():
            # Open history in a tmux split (top half, read-only).
            result = subprocess.run(
                [
                    "tmux", "split-window", "-v", "-b", "-l", "50%",
                    "-P", "-F", "#{pane_id}",
                    f"less +G '{hist_file}'",
                ],
                capture_output=True,
                text=True,
            )
            hist_pane = result.stdout.strip() or None

        # Open editor in a tmux split pane; signal channel on exit.
        start = _comment_end_line(tmpfile.read_text())
        start_flag = f" +{start}" if start and ed in ("vim", "nvim") else ""
        subprocess.run(
            [
                "tmux", "split-window", "-v",
                f"{ed}{start_flag} '{tmpfile}'; tmux wait-for -S {channel}",
            ],
            check=False,
        )

        # Block until the editor exits.
        subprocess.run(["tmux", "wait-for", channel], check=False)

        return tmpfile.read_text()
    finally:
        if hist_pane:
            subprocess.run(
                ["tmux", "kill-pane", "-t", hist_pane],
                capture_output=True,
                check=False,
            )


def edit_prompt(
    initial_content: str = "",
    hist_file: Path | None = None,
    *,
    editor: str | None = None,
    tty: str = "/dev/tty",
) -> str:
    """Open $EDITOR so the user can compose a prompt.

    When running inside tmux, delegates to :func:`_edit_prompt_tmux` which uses
    ``tmux split-window`` to open the editor in a separate pane.  For
    continue/resume sessions the history is shown in a companion tmux pane
    instead of a vim internal split.

    When *not* in tmux, falls back to the original behaviour: vim/nvim opens a
    split window (history read-only on top, prompt editable on bottom) when
    *hist_file* is supplied; other editors get a plain single-file view.

    If *initial_content* starts with ``#`` comment lines, vim/nvim will place
    the cursor on the first non-comment line.

    Returns the full text of the edited file (may be empty / whitespace-only).
    """
    ed = editor or os.environ.get("EDITOR", "vim")
    fd, tmp = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(initial_content)
        tmpfile = Path(tmp)

        if _in_tmux():
            return _edit_prompt_tmux(ed, tmpfile, hist_file)

        # ── Non-tmux fallback ─────────────────────────────────────────────
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
                start = _comment_end_line(initial_content)
                cmd = [ed]
                if start and ed in ("vim", "nvim"):
                    cmd.append(f"+{start}")
                cmd.append(str(tmpfile))
                subprocess.run(cmd, stdin=tty_stdin)
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


# ── Slug generation ───────────────────────────────────────────────────────────


def _sanitize_slug(raw: str) -> str:
    """Convert a raw string to a kebab-case filename slug (max 60 chars)."""
    slug = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    # Keep at most 5 words, 60 chars.
    parts = slug.split("-")
    if len(parts) > 5:
        slug = "-".join(parts[:5])
    return slug[:60]


def generate_slug(
    prompt: str,
    project_dir: Path,
    *,
    claude_bin: str = "claude",
) -> str:
    """Generate a semantic kebab-case slug for a new session file.

    Calls the Claude CLI to produce a 2-4 word summary of the session.
    Falls back to a timestamp-based name on any failure.
    """
    slug_prompt = (
        "Generate a 2-4 word kebab-case slug summarizing this coding session.\n"
        f"Project: {project_dir.name}\n"
        f"User prompt: {prompt[:300]}\n\n"
        "Reply with ONLY the slug (e.g. fix-auth-bug). No quotes, no explanation."
    )
    try:
        result = subprocess.run(
            [claude_bin, "--print"],
            input=slug_prompt,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            slug = _sanitize_slug(result.stdout)
            if slug:
                return slug
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # Fallback: timestamp-based name.
    from datetime import datetime

    return datetime.now().strftime("session-%H%M%S")


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
    session_id: str | None = None,
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
            f.write(f"session: {session_id or hist_file.stem}\n")
            f.write(f"project: {project_dir}\n")
            f.write(f"date: {date.today().isoformat()}\n")
            f.write("---\n\n")
        f.write(f"> {prompt}\n\n")
        f.write(md_content)


# ── Argument parsing ──────────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse cl-specific flags; return (namespace, args_to_forward_to_claude).

    - ``-c`` / ``--continue`` → stored in namespace only; ``main()`` decides
      whether to add ``--continue`` or ``--resume <id>`` based on project
      session history.
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

    # Neither --continue nor --resume is forwarded here; main() decides
    # whether to add --continue or --resume <id> based on project history.
    claude_passthrough: list[str] = list(extra)

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
        resume_id = (
            _parse_front_matter(session_file).get("session") or session_file.stem
        )
        ns.continue_session = True  # open split-view just like -c

    # ── Project-specific continue ─────────────────────────────────────────────
    if ns.continue_session and session_file is None:
        project_sessions = find_project_sessions(sessions_base, Path.cwd())
        if project_sessions:
            session_file = project_sessions[0]
            resume_id = _parse_front_matter(session_file).get("session")
        else:
            # No project session found — fall back to claude's --continue.
            claude_passthrough.append("--continue")

    # ── Prepend session summary comment when continuing/resuming ──────────
    if session_file is not None:
        initial_content = _session_summary_comment(session_file) + initial_content

    # ── Edit prompt ───────────────────────────────────────────────────────────
    raw_prompt = edit_prompt(initial_content)
    prompt = _strip_leading_comments(raw_prompt)
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
            # Append to an existing file for this session if present (e.g.
            # cross-day continuation), otherwise generate a slug and start a
            # new file in today's date dir.
            target = find_session_file(sessions_base, session_id)
            if target is None:
                slug = generate_slug(prompt, Path.cwd())
                target = today_dir / f"{slug}.md"
                i = 2
                while target.exists():
                    target = today_dir / f"{slug}-{i}.md"
                    i += 1
            append_to_history(
                target, prompt, tmpmd,
                project_dir=Path.cwd(), session_id=session_id,
            )
    finally:
        tmpjson.unlink(missing_ok=True)
        tmpmd.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
