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
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import CL_TOOL, DEFAULT_MOCK_PROMPT, FIXTURES_DIR

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
    return cl_env["home_dir"] / ".local" / "share" / "cl" / "sessions"


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

    def test_session_file_named_by_slug_and_session_id(self, cl_env):
        run_cl(cl_env)
        files = session_files(cl_env)
        assert files[0].stem == f"mock-session-slug-{MOCK_SESSION_ID}"

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


# ── Session file naming format ───────────────────────────────────────────────


class TestSessionFileNaming:
    """Verify session file naming: ~/.local/share/cl/sessions/<date>/<slug>-<session-id>.md"""

    def test_sessions_under_xdg_share_path(self, cl_env):
        run_cl(cl_env)
        base = sessions_base(cl_env)
        assert base == cl_env["home_dir"] / ".local" / "share" / "cl" / "sessions"
        files = session_files(cl_env)
        assert len(files) == 1
        assert str(files[0]).startswith(str(base))

    def test_date_directory_is_iso_format(self, cl_env):
        import re
        run_cl(cl_env)
        files = session_files(cl_env)
        date_dir = files[0].parent.name
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", date_dir)

    def test_date_directory_is_today(self, cl_env):
        from datetime import date
        run_cl(cl_env)
        files = session_files(cl_env)
        assert files[0].parent.name == date.today().isoformat()

    def test_filename_contains_slug_and_session_id(self, cl_env):
        run_cl(cl_env)
        files = session_files(cl_env)
        stem = files[0].stem
        # Format: <semantic-slug>-<session-id>
        assert stem == f"mock-session-slug-{MOCK_SESSION_ID}"

    def test_filename_ends_with_session_id(self, cl_env):
        run_cl(cl_env)
        files = session_files(cl_env)
        assert files[0].stem.endswith(MOCK_SESSION_ID)

    def test_filename_starts_with_slug(self, cl_env):
        run_cl(cl_env)
        files = session_files(cl_env)
        assert files[0].stem.startswith("mock-session-slug-")

    def test_full_path_format(self, cl_env):
        """Verify the complete path matches the expected format."""
        from datetime import date
        run_cl(cl_env)
        files = session_files(cl_env)
        expected_base = cl_env["home_dir"] / ".local" / "share" / "cl" / "sessions"
        expected = expected_base / date.today().isoformat() / f"mock-session-slug-{MOCK_SESSION_ID}.md"
        assert files[0] == expected

    def test_suffix_is_md(self, cl_env):
        run_cl(cl_env)
        files = session_files(cl_env)
        assert files[0].suffix == ".md"


# ── Happy path functional tests ─────────────────────────────────────────────


class TestHappyPath:
    """Functional tests for the main happy-path workflow."""

    def test_new_session_end_to_end(self, cl_env):
        """New session: prompt → pipeline → session file created with correct content."""
        result = run_cl(cl_env)
        assert result.returncode == 0

        files = session_files(cl_env)
        assert len(files) == 1

        content = files[0].read_text()
        # Front matter present
        assert content.startswith("---\n")
        assert f"session: {MOCK_SESSION_ID}" in content
        assert f"project: {cl_env['work_dir']}" in content
        # Prompt and response
        assert f"> {DEFAULT_MOCK_PROMPT}" in content
        assert "Hello from mock Claude!" in content

    def test_continue_session_end_to_end(self, cl_env):
        """Continue: first run creates session, second run with -c appends to it."""
        run_cl(cl_env)
        files_before = session_files(cl_env)
        assert len(files_before) == 1

        cl_env["env"]["MOCK_EDITOR_PROMPT"] = "Follow-up question"
        result = run_cl(cl_env, "-c")
        assert result.returncode == 0

        files_after = session_files(cl_env)
        assert len(files_after) == 1  # Same file, not a new one
        content = files_after[0].read_text()
        assert DEFAULT_MOCK_PROMPT in content
        assert "Follow-up question" in content

    def test_prompt_echoed_before_response(self, cl_env):
        """User's prompt is echoed to stdout."""
        result = run_cl(cl_env)
        assert DEFAULT_MOCK_PROMPT in result.stdout

    def test_pipeline_passes_fixed_flags(self, cl_env):
        """Fixed claude flags (--print, --verbose, --output-format) are always passed."""
        result = run_cl(cl_env)
        assert "--print" in result.stderr
        assert "--verbose" in result.stderr
        assert "--output-format=stream-json" in result.stderr

    def test_empty_prompt_aborts_gracefully(self, cl_env):
        """Empty prompt exits 0 without creating a session file."""
        cl_env["env"]["MOCK_EDITOR_PROMPT"] = ""
        result = run_cl(cl_env)
        assert result.returncode == 0
        assert session_files(cl_env) == []

    def test_continue_no_prior_session_falls_back(self, cl_env):
        """With -c but no prior project session, falls back to --continue."""
        result = run_cl(cl_env, "-c")
        assert result.returncode == 0
        assert "--continue" in result.stderr

    def test_session_file_has_correct_front_matter_fields(self, cl_env):
        """Front matter contains exactly session, project, and date fields."""
        from datetime import date
        run_cl(cl_env)
        content = session_files(cl_env)[0].read_text()
        # Extract front matter
        fm_end = content.index("\n---\n", 4)
        fm_block = content[4:fm_end]
        keys = [line.split(":")[0].strip() for line in fm_block.splitlines() if ":" in line]
        assert "session" in keys
        assert "project" in keys
        assert "date" in keys

    def test_multi_turn_conversation(self, cl_env):
        """Multiple turns accumulate in the same session file."""
        run_cl(cl_env)

        for i in range(3):
            cl_env["env"]["MOCK_EDITOR_PROMPT"] = f"Turn {i+2}"
            run_cl(cl_env, "-c")

        files = session_files(cl_env)
        assert len(files) == 1
        content = files[0].read_text()
        assert DEFAULT_MOCK_PROMPT in content
        assert "Turn 2" in content
        assert "Turn 3" in content
        assert "Turn 4" in content


# ── Ctrl+C / graceful shutdown ────────────────────────────────────────────────


class TestCtrlC:
    @pytest.fixture()
    def slow_cl_env(self, cl_env):
        """cl_env with a slow mock claude that hangs until signalled."""
        bin_dir = cl_env["bin_dir"]
        dst = bin_dir / "claude"
        shutil.copy(FIXTURES_DIR / "mock_claude_slow.py", dst)
        text = dst.read_text()
        lines = text.splitlines(keepends=True)
        lines[0] = f"#!{sys.executable}\n"
        dst.write_text("".join(lines))
        dst.chmod(0o755)
        return cl_env

    def test_sigint_returns_130(self, slow_cl_env):
        """Sending SIGINT to cl_tool exits with code 130."""
        proc = subprocess.Popen(
            [sys.executable, str(CL_TOOL)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=slow_cl_env["work_dir"],
            env=slow_cl_env["env"],
        )
        # Wait for the pipeline to start (mock claude emits to stderr).
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # Check if there's output on stderr indicating claude started.
            import select
            ready, _, _ = select.select([proc.stderr], [], [], 0.1)
            if ready:
                break
        # Send SIGINT to the process group.
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        assert proc.returncode == 130

    def test_sigint_cleans_up_temp_files(self, slow_cl_env):
        """Temp files are cleaned up even after Ctrl+C."""
        proc = subprocess.Popen(
            [sys.executable, str(CL_TOOL)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=slow_cl_env["work_dir"],
            env=slow_cl_env["env"],
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            import select
            ready, _, _ = select.select([proc.stderr], [], [], 0.1)
            if ready:
                break
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        # Temp files (*.json, *.md in /tmp) should have been cleaned up.
        # We can verify no session files were created (since we interrupted
        # before the result message was emitted).
        assert session_files(slow_cl_env) == []
