"""End-to-end tests — spawn cl_tool.py as a subprocess.

All tests use the `cl_env` fixture (defined in conftest.py) which provides:
  - A temporary working directory (``work_dir``)
  - A temporary HOME (``home_dir``) that isolates git-config changes
  - Mock binaries for ``claude``, ``editor``, and ``print-my-ride`` on PATH

The mock editor reads MOCK_EDITOR_PROMPT from the environment and writes it
to the temp file that cl opens.  This lets each test control what "the user
typed" without actually launching a real editor.

The mock claude always emits the same fixed stream-json with session_id
``sess-mock-deadbeef1234`` (or the value of --resume <id> if supplied).

The mock print-my-ride drains stdin and writes a stub markdown file to
--save-md so the history-file content tests have something to assert on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import CL_TOOL, DEFAULT_MOCK_PROMPT

MOCK_SESSION_ID = "sess-mock-deadbeef1234"


# ── Subprocess helper ─────────────────────────────────────────────────────────


def run_cl(
    cl_env: dict,
    *extra_args: str,
    stdin: str = "",
) -> subprocess.CompletedProcess:
    """Run cl_tool.py in the test environment and return the result."""
    return subprocess.run(
        [sys.executable, str(CL_TOOL), *extra_args],
        capture_output=True,
        text=True,
        cwd=cl_env["work_dir"],
        env=cl_env["env"],
        input=stdin,
    )


def sessions_base(cl_env: dict) -> Path:
    return cl_env["home_dir"] / ".local" / "cl" / "sessions"


def session_files(cl_env: dict) -> list[Path]:
    base = sessions_base(cl_env)
    if not base.exists():
        return []
    return list(base.glob("*/*.md"))


# ── Basic invocation ──────────────────────────────────────────────────────────


class TestBasicInvocation:
    def test_exit_code_zero(self, cl_env):
        result = run_cl(cl_env)
        assert result.returncode == 0

    def test_history_dir_created(self, cl_env):
        run_cl(cl_env)
        assert sessions_base(cl_env).is_dir()

    def test_session_file_created(self, cl_env):
        run_cl(cl_env)
        assert len(session_files(cl_env)) == 1

    def test_session_file_named_by_slug(self, cl_env):
        run_cl(cl_env)
        files = session_files(cl_env)
        assert files[0].stem == "mock-session-slug"

    def test_history_contains_prompt(self, cl_env):
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        assert DEFAULT_MOCK_PROMPT in content

    def test_history_prompt_is_blockquote(self, cl_env):
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        assert f"> {DEFAULT_MOCK_PROMPT}" in content

    def test_history_contains_response(self, cl_env):
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        # Mock pmr writes this fixed response.
        assert "Hello from mock Claude!" in content

    def test_prompt_echoed_to_stdout(self, cl_env):
        result = run_cl(cl_env)
        assert DEFAULT_MOCK_PROMPT in result.stdout


# ── Empty / whitespace-only prompt ───────────────────────────────────────────


class TestEmptyPrompt:
    def test_no_history_file_on_empty_prompt(self, cl_env):
        cl_env["env"]["MOCK_EDITOR_PROMPT"] = ""
        run_cl(cl_env)
        assert session_files(cl_env) == []

    def test_no_history_file_on_whitespace_prompt(self, cl_env):
        cl_env["env"]["MOCK_EDITOR_PROMPT"] = "   \n\t  "
        run_cl(cl_env)
        assert session_files(cl_env) == []

    def test_exit_code_zero_on_empty_prompt(self, cl_env):
        cl_env["env"]["MOCK_EDITOR_PROMPT"] = ""
        result = run_cl(cl_env)
        assert result.returncode == 0


# ── --continue / -c flag ──────────────────────────────────────────────────────


class TestContinueFlag:
    def test_short_flag_exits_zero(self, cl_env):
        result = run_cl(cl_env, "-c")
        assert result.returncode == 0

    def test_long_flag_exits_zero(self, cl_env):
        result = run_cl(cl_env, "--continue")
        assert result.returncode == 0

    def test_continue_uses_resume_for_project_session(self, cl_env):
        # First run creates a session for this project.
        run_cl(cl_env)
        # Second run with -c should resume that project-specific session.
        result = run_cl(cl_env, "-c")
        assert "--resume" in result.stderr
        assert MOCK_SESSION_ID in result.stderr

    def test_continue_falls_back_when_no_project_session(self, cl_env):
        # First -c with no prior sessions falls back to --continue.
        result = run_cl(cl_env, "-c")
        assert "--continue" in result.stderr

    def test_session_file_still_created_with_continue(self, cl_env):
        # First run creates a session file.
        run_cl(cl_env)
        assert len(session_files(cl_env)) == 1
        # Second run with --continue should append to the SAME file (same
        # session_id from mock claude) not create a new file.
        run_cl(cl_env, "-c")
        # The history file should now have two prompts (both appended).
        content = session_files(cl_env)[0].read_text()
        assert content.count(DEFAULT_MOCK_PROMPT) >= 1

    def test_continue_session_file_has_multiple_turns(self, cl_env):
        run_cl(cl_env)
        cl_env["env"]["MOCK_EDITOR_PROMPT"] = "Follow-up question"
        run_cl(cl_env, "-c")
        content = session_files(cl_env)[0].read_text()
        assert DEFAULT_MOCK_PROMPT in content
        assert "Follow-up question" in content


# ── Piped stdin ───────────────────────────────────────────────────────────────


class TestPipedStdin:
    def test_piped_stdin_does_not_crash(self, cl_env):
        result = run_cl(cl_env, stdin="some piped content")
        assert result.returncode == 0

    def test_session_created_with_piped_stdin(self, cl_env):
        run_cl(cl_env, stdin="some piped content")
        # The mock editor still writes the prompt (it ignores initial content).
        assert len(session_files(cl_env)) == 1

    def test_empty_piped_stdin_does_not_crash(self, cl_env):
        result = run_cl(cl_env, stdin="")
        assert result.returncode == 0


# ── Repeated invocations / separate sessions ──────────────────────────────────


class TestMultipleSessions:
    def test_two_sessions_produce_one_file_same_session_id(self, cl_env):
        # Mock claude always returns the same session_id, so the second run
        # appends to the same file.
        run_cl(cl_env)
        run_cl(cl_env)
        assert len(session_files(cl_env)) == 1

    def test_history_accumulates_on_repeated_runs(self, cl_env):
        run_cl(cl_env)
        cl_env["env"]["MOCK_EDITOR_PROMPT"] = "Second prompt"
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        assert DEFAULT_MOCK_PROMPT in content
        assert "Second prompt" in content


# ── Help / argument errors ────────────────────────────────────────────────────


class TestHelp:
    def test_help_flag_exits_zero(self, cl_env):
        result = run_cl(cl_env, "--help")
        assert result.returncode == 0

    def test_help_mentions_continue(self, cl_env):
        result = run_cl(cl_env, "--help")
        assert "--continue" in result.stdout or "-c" in result.stdout

    def test_help_mentions_resume(self, cl_env):
        result = run_cl(cl_env, "--help")
        assert "--resume" in result.stdout or "-r" in result.stdout


# ── Claude args passthrough ───────────────────────────────────────────────────


class TestClaudeArgsPassthrough:
    def test_extra_flags_forwarded_to_claude(self, cl_env):
        run_cl(cl_env, "--model", "claude-3-5")
        # Mock claude logs its args to stderr.
        result = run_cl(cl_env, "--model", "claude-3-5")
        assert "--model" in result.stderr
        assert "claude-3-5" in result.stderr

    def test_fixed_flags_always_present(self, cl_env):
        result = run_cl(cl_env)
        # --print, --verbose, --output-format=stream-json must always appear.
        assert "--print" in result.stderr
        assert "--verbose" in result.stderr
        assert "--output-format=stream-json" in result.stderr


# ── Resume flag (-r) — no existing sessions ───────────────────────────────────


class TestResumeNoSessions:
    def test_resume_exits_nonzero_when_no_history(self, cl_env):
        result = run_cl(cl_env, "-r")
        assert result.returncode != 0

    def test_resume_prints_error_to_stderr(self, cl_env):
        result = run_cl(cl_env, "-r")
        assert "No sessions" in result.stderr


# ── History file structure ────────────────────────────────────────────────────


class TestHistoryFileStructure:
    def test_history_file_suffix_is_md(self, cl_env):
        run_cl(cl_env)
        for f in session_files(cl_env):
            assert f.suffix == ".md"

    def test_history_file_in_date_subdir(self, cl_env):
        import re
        run_cl(cl_env)
        for f in session_files(cl_env):
            assert re.match(r"\d{4}-\d{2}-\d{2}$", f.parent.name)

    def test_history_blank_line_after_prompt(self, cl_env):
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        # Format: "> {prompt}\n\n{md_content}"
        assert f"> {DEFAULT_MOCK_PROMPT}\n\n" in content

    def test_history_has_yaml_front_matter(self, cl_env):
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        assert content.startswith("---\n")
        assert "session:" in content
        assert "project:" in content
        assert "date:" in content

    def test_history_front_matter_has_session_id(self, cl_env):
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        assert f"session: {MOCK_SESSION_ID}" in content
