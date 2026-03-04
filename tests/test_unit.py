"""Unit tests for cl_tool helper functions.

No subprocesses are spawned here — each function is tested in isolation
with mocks where external I/O is required.
"""

from __future__ import annotations

import configparser
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from cl_tool import (
    _GIT_EXCLUDE_ENTRY,
    _parse_front_matter,
    _read_global_excludes_file,
    _sanitize_slug,
    append_to_history,
    build_stdin_block,
    extract_session_id,
    find_project_sessions,
    find_session_file,
    find_sessions,
    generate_slug,
    main,
    parse_args,
    run_pipeline,
    select_session_menu,
    setup_git_excludes,
)


# ── build_stdin_block ─────────────────────────────────────────────────────────


class TestBuildStdinBlock:
    def test_wraps_in_code_fence(self):
        result = build_stdin_block("hello world")
        assert "```" in result
        assert "hello world" in result

    def test_starts_with_double_newline(self):
        result = build_stdin_block("x")
        assert result.startswith("\n\n")

    def test_ends_with_newline(self):
        result = build_stdin_block("x")
        assert result.endswith("\n")

    def test_exact_format(self):
        result = build_stdin_block("some code")
        assert result == "\n\n```\nsome code\n```\n"

    def test_multiline_content_preserved(self):
        content = "line1\nline2\nline3"
        result = build_stdin_block(content)
        assert "line1\nline2\nline3" in result

    def test_empty_content(self):
        result = build_stdin_block("")
        assert result == "\n\n```\n\n```\n"


# ── extract_session_id ────────────────────────────────────────────────────────


class TestExtractSessionId:
    def test_returns_first_session_id(self, tmp_path):
        jf = tmp_path / "out.json"
        jf.write_text(
            json.dumps({"type": "system", "session_id": "sess-abc"}) + "\n"
            + json.dumps({"type": "result", "session_id": "sess-abc"}) + "\n"
        )
        assert extract_session_id(jf) == "sess-abc"

    def test_skips_entries_without_session_id(self, tmp_path):
        jf = tmp_path / "out.json"
        jf.write_text(
            json.dumps({"type": "text"}) + "\n"
            + json.dumps({"session_id": "sess-xyz"}) + "\n"
        )
        assert extract_session_id(jf) == "sess-xyz"

    def test_returns_none_when_no_session_id(self, tmp_path):
        jf = tmp_path / "out.json"
        jf.write_text(json.dumps({"type": "event"}) + "\n")
        assert extract_session_id(jf) is None

    def test_returns_none_for_empty_file(self, tmp_path):
        jf = tmp_path / "empty.json"
        jf.write_text("")
        assert extract_session_id(jf) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert extract_session_id(tmp_path / "nonexistent.json") is None

    def test_skips_malformed_lines(self, tmp_path):
        jf = tmp_path / "out.json"
        jf.write_text(
            "not-json\n"
            + json.dumps({"session_id": "sess-good"}) + "\n"
        )
        assert extract_session_id(jf) == "sess-good"

    def test_skips_blank_lines(self, tmp_path):
        jf = tmp_path / "out.json"
        jf.write_text(
            "\n\n"
            + json.dumps({"session_id": "sess-ok"}) + "\n"
        )
        assert extract_session_id(jf) == "sess-ok"


# ── find_sessions ─────────────────────────────────────────────────────────────


class TestFindSessions:
    def _date_dir(self, base: Path, name: str = "2026-01-01") -> Path:
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_returns_md_files_only(self, tmp_path):
        d = self._date_dir(tmp_path)
        (d / "a.md").write_text("a")
        (d / "b.txt").write_text("b")
        sessions = find_sessions(tmp_path)
        assert all(p.suffix == ".md" for p in sessions)

    def test_sorted_newest_first(self, tmp_path):
        d = self._date_dir(tmp_path)
        old = d / "old.md"
        old.write_text("old")
        # Give the filesystem a moment to record distinct mtimes.
        time.sleep(0.02)
        new = d / "new.md"
        new.write_text("new")
        sessions = find_sessions(tmp_path)
        assert sessions[0] == new
        assert sessions[1] == old

    def test_empty_directory(self, tmp_path):
        assert find_sessions(tmp_path) == []

    def test_multiple_files_order(self, tmp_path):
        d = self._date_dir(tmp_path)
        files = []
        for i in range(3):
            p = d / f"sess-{i}.md"
            p.write_text(str(i))
            files.append(p)
            time.sleep(0.02)
        sessions = find_sessions(tmp_path)
        # newest first
        assert [p.name for p in sessions] == ["sess-2.md", "sess-1.md", "sess-0.md"]

    def test_searches_across_date_subdirs(self, tmp_path):
        d1 = self._date_dir(tmp_path, "2026-01-01")
        d2 = self._date_dir(tmp_path, "2026-01-02")
        (d1 / "old.md").write_text("old")
        time.sleep(0.02)
        (d2 / "new.md").write_text("new")
        sessions = find_sessions(tmp_path)
        assert len(sessions) == 2
        assert sessions[0].parent.name == "2026-01-02"

    def test_ignores_md_files_at_top_level(self, tmp_path):
        # files not inside a date subdir should not be returned
        (tmp_path / "stray.md").write_text("x")
        assert find_sessions(tmp_path) == []


# ── select_session_menu ───────────────────────────────────────────────────────


class TestSelectSessionMenu:
    def _sessions(self, tmp_path, names=("a.md", "b.md", "c.md")):
        paths = []
        for name in names:
            p = tmp_path / name
            p.write_text("")
            paths.append(p)
        return paths

    def test_valid_choice_returns_correct_path(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path)
        result = select_session_menu(sessions, _input_fn=lambda: "1")
        assert result == sessions[0]

    def test_choice_two(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path)
        result = select_session_menu(sessions, _input_fn=lambda: "2")
        assert result == sessions[1]

    def test_out_of_range_returns_none(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path)
        result = select_session_menu(sessions, _input_fn=lambda: "99")
        assert result is None

    def test_non_numeric_returns_none(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path)
        result = select_session_menu(sessions, _input_fn=lambda: "abc")
        assert result is None

    def test_zero_returns_none(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path)
        result = select_session_menu(sessions, _input_fn=lambda: "0")
        assert result is None

    def test_empty_sessions_returns_none(self, tmp_path, capsys):
        result = select_session_menu([], _input_fn=lambda: "1")
        assert result is None

    def test_prints_session_names_to_stderr(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path, ["sess-aaa.md", "sess-bbb.md"])
        select_session_menu(sessions, _input_fn=lambda: "1")
        err = capsys.readouterr().err
        assert "sess-aaa.md" in err
        assert "sess-bbb.md" in err

    def test_prints_parent_dir_to_stderr(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path, ["sess-aaa.md"])
        select_session_menu(sessions, _input_fn=lambda: "1")
        err = capsys.readouterr().err
        assert tmp_path.name in err

    def test_numbered_list_in_stderr(self, tmp_path, capsys):
        sessions = self._sessions(tmp_path, ["x.md"])
        select_session_menu(sessions, _input_fn=lambda: "1")
        err = capsys.readouterr().err
        assert "1)" in err


# ── parse_args ────────────────────────────────────────────────────────────────


class TestParseArgs:
    def test_continue_short_flag(self):
        ns, _ = parse_args(["-c"])
        assert ns.continue_session is True

    def test_continue_long_flag(self):
        ns, _ = parse_args(["--continue"])
        assert ns.continue_session is True

    def test_resume_short_flag(self):
        ns, _ = parse_args(["-r"])
        assert ns.resume_session is True

    def test_resume_long_flag(self):
        ns, _ = parse_args(["--resume"])
        assert ns.resume_session is True

    def test_continue_not_forwarded_by_parse_args(self):
        _, claude_args = parse_args(["-c"])
        assert "--continue" not in claude_args

    def test_resume_not_forwarded_to_claude(self):
        _, claude_args = parse_args(["-r"])
        assert "--resume" not in claude_args
        assert "-r" not in claude_args

    def test_unknown_args_forwarded(self):
        _, claude_args = parse_args(["--model", "claude-3-5"])
        assert "--model" in claude_args
        assert "claude-3-5" in claude_args

    def test_no_flags(self):
        ns, claude_args = parse_args([])
        assert ns.continue_session is False
        assert ns.resume_session is False
        assert claude_args == []

    def test_continue_sets_flag_only(self):
        # -c sets the namespace flag but does not add --continue to passthrough.
        ns, claude_args = parse_args(["-c"])
        assert ns.continue_session is True
        assert "--continue" not in claude_args

    def test_mixed_cl_and_claude_args(self):
        ns, claude_args = parse_args(["-c", "--model", "claude-3", "--verbose"])
        assert ns.continue_session is True
        assert "--model" in claude_args
        assert "claude-3" in claude_args
        assert "--verbose" in claude_args
        assert "--continue" not in claude_args

    def test_resume_does_not_set_continue(self):
        ns, _ = parse_args(["-r"])
        assert ns.continue_session is False


# ── setup_git_excludes ────────────────────────────────────────────────────────


class TestSetupGitExcludes:
    def _fake_home(self, tmp_path) -> Path:
        h = tmp_path / "home"
        h.mkdir(exist_ok=True)
        return h

    def _write_gitconfig(self, home: Path, excludes_path: Path) -> None:
        gitconfig = home / ".gitconfig"
        gitconfig.write_text(
            "[core]\n"
            f"\texcludesFile = {excludes_path}\n"
        )

    def test_adds_entry_to_existing_file(self, tmp_path, monkeypatch):
        home = self._fake_home(tmp_path)
        excludes = tmp_path / "gitignore_global"
        excludes.write_text("*.pyc\n")
        self._write_gitconfig(home, excludes)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        setup_git_excludes()
        assert _GIT_EXCLUDE_ENTRY in excludes.read_text().splitlines()

    def test_creates_excludes_file_if_absent(self, tmp_path, monkeypatch):
        home = self._fake_home(tmp_path)
        excludes = tmp_path / "gitignore_global"
        self._write_gitconfig(home, excludes)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert not excludes.exists()
        setup_git_excludes()
        assert excludes.exists()
        assert _GIT_EXCLUDE_ENTRY in excludes.read_text()

    def test_idempotent(self, tmp_path, monkeypatch):
        home = self._fake_home(tmp_path)
        excludes = tmp_path / "gitignore_global"
        excludes.write_text(_GIT_EXCLUDE_ENTRY + "\n")
        self._write_gitconfig(home, excludes)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        setup_git_excludes()
        setup_git_excludes()
        assert excludes.read_text().count(_GIT_EXCLUDE_ENTRY) == 1

    def test_creates_default_excludes_when_no_gitconfig(self, tmp_path, monkeypatch):
        home = self._fake_home(tmp_path)
        # No .gitconfig
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        with patch("cl_tool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            setup_git_excludes()
        # Should have called git config --global once.
        mock_run.assert_called_once()
        call_cmd = mock_run.call_args[0][0]
        assert "git" in call_cmd
        assert "config" in call_cmd
        # Default excludes file should be created.
        default = home / ".gitignore_global"
        assert default.exists()
        assert _GIT_EXCLUDE_ENTRY in default.read_text()

    def test_preserves_existing_entries(self, tmp_path, monkeypatch):
        home = self._fake_home(tmp_path)
        excludes = tmp_path / "gitignore_global"
        excludes.write_text("*.pyc\n*.egg-info/\n")
        self._write_gitconfig(home, excludes)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        setup_git_excludes()
        content = excludes.read_text()
        assert "*.pyc" in content
        assert "*.egg-info/" in content
        assert _GIT_EXCLUDE_ENTRY in content


# ── _read_global_excludes_file ────────────────────────────────────────────────


class TestReadGlobalExcludesFile:
    def test_returns_path_from_gitconfig(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        excludes = tmp_path / "my_excludes"
        gitconfig = home / ".gitconfig"
        gitconfig.write_text(f"[core]\n\texcludesFile = {excludes}\n")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        result = _read_global_excludes_file()
        assert result == excludes

    def test_returns_none_when_no_gitconfig(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert _read_global_excludes_file() is None

    def test_returns_none_when_no_core_section(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = Test\n")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert _read_global_excludes_file() is None

    def test_expands_tilde_in_path(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        gitconfig = home / ".gitconfig"
        gitconfig.write_text("[core]\n\texcludesFile = ~/.gitignore_global\n")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        result = _read_global_excludes_file()
        assert result is not None
        assert "~" not in str(result)


# ── append_to_history ─────────────────────────────────────────────────────────


class TestAppendToHistory:
    def test_creates_new_file(self, tmp_path):
        hist = tmp_path / "sess-abc.md"
        md = tmp_path / "resp.md"
        md.write_text("## Answer\n\nFour.\n")
        append_to_history(hist, "What is 2+2?", md)
        assert hist.exists()

    def test_prompt_in_file(self, tmp_path):
        hist = tmp_path / "sess.md"
        md = tmp_path / "resp.md"
        md.write_text("answer")
        append_to_history(hist, "My question", md)
        assert "> My question" in hist.read_text()

    def test_response_in_file(self, tmp_path):
        hist = tmp_path / "sess.md"
        md = tmp_path / "resp.md"
        md.write_text("My response content")
        append_to_history(hist, "q", md)
        assert "My response content" in hist.read_text()

    def test_appends_on_second_call(self, tmp_path):
        hist = tmp_path / "sess.md"
        md1 = tmp_path / "r1.md"
        md1.write_text("answer1")
        md2 = tmp_path / "r2.md"
        md2.write_text("answer2")
        append_to_history(hist, "q1", md1)
        append_to_history(hist, "q2", md2)
        content = hist.read_text()
        assert "q1" in content
        assert "q2" in content
        assert "answer1" in content
        assert "answer2" in content

    def test_handles_missing_md_file(self, tmp_path):
        hist = tmp_path / "sess.md"
        md = tmp_path / "nonexistent.md"
        append_to_history(hist, "question", md)
        assert "> question" in hist.read_text()

    def test_format_prompt_then_blank_line_then_content(self, tmp_path):
        hist = tmp_path / "sess.md"
        md = tmp_path / "resp.md"
        md.write_text("response")
        append_to_history(hist, "prompt text", md)
        content = hist.read_text()
        assert content.startswith("> prompt text\n\n")

    def test_session_id_in_front_matter(self, tmp_path):
        hist = tmp_path / "my-slug.md"
        md = tmp_path / "resp.md"
        md.write_text("answer")
        append_to_history(
            hist, "question", md,
            project_dir=Path("/project"), session_id="sess-abc123",
        )
        content = hist.read_text()
        assert "session: sess-abc123" in content

    def test_session_id_defaults_to_stem(self, tmp_path):
        hist = tmp_path / "sess-xyz.md"
        md = tmp_path / "resp.md"
        md.write_text("answer")
        append_to_history(hist, "q", md, project_dir=Path("/project"))
        content = hist.read_text()
        assert "session: sess-xyz" in content


# ── _parse_front_matter ────────────────────────────────────────────────────────


class TestParseFrontMatter:
    def test_parses_standard_front_matter(self, tmp_path):
        f = tmp_path / "sess.md"
        f.write_text(
            "---\n"
            "session: abc123\n"
            "project: /tmp/work\n"
            "date: 2026-03-03\n"
            "---\n\ncontent"
        )
        result = _parse_front_matter(f)
        assert result == {
            "session": "abc123",
            "project": "/tmp/work",
            "date": "2026-03-03",
        }

    def test_returns_empty_dict_for_no_front_matter(self, tmp_path):
        f = tmp_path / "sess.md"
        f.write_text("Just content")
        assert _parse_front_matter(f) == {}

    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        assert _parse_front_matter(tmp_path / "nonexistent.md") == {}

    def test_returns_empty_dict_for_unclosed_front_matter(self, tmp_path):
        f = tmp_path / "sess.md"
        f.write_text("---\nsession: abc\n")
        assert _parse_front_matter(f) == {}

    def test_empty_file(self, tmp_path):
        f = tmp_path / "sess.md"
        f.write_text("")
        assert _parse_front_matter(f) == {}


# ── _sanitize_slug ────────────────────────────────────────────────────────────


class TestSanitizeSlug:
    def test_basic_slug(self):
        assert _sanitize_slug("fix auth bug") == "fix-auth-bug"

    def test_strips_special_chars(self):
        assert _sanitize_slug("fix-auth-bug!@#$%") == "fix-auth-bug"

    def test_lowercases(self):
        assert _sanitize_slug("Fix Auth Bug") == "fix-auth-bug"

    def test_limits_words(self):
        result = _sanitize_slug("one two three four five six seven")
        assert result.count("-") <= 4  # max 5 words = 4 hyphens

    def test_strips_leading_trailing_hyphens(self):
        assert _sanitize_slug("-hello-world-") == "hello-world"

    def test_empty_string(self):
        assert _sanitize_slug("") == ""

    def test_collapses_hyphens(self):
        assert _sanitize_slug("fix---auth---bug") == "fix-auth-bug"


# ── find_session_file ─────────────────────────────────────────────────────────


class TestFindSessionFile:
    def test_finds_by_front_matter(self, tmp_path):
        d = tmp_path / "2026-03-03"
        d.mkdir()
        f = d / "fix-bug.md"
        f.write_text("---\nsession: sess-abc\nproject: /work\ndate: 2026-03-03\n---\n")
        result = find_session_file(tmp_path, "sess-abc")
        assert result == f

    def test_returns_none_when_not_found(self, tmp_path):
        d = tmp_path / "2026-03-03"
        d.mkdir()
        f = d / "fix-bug.md"
        f.write_text("---\nsession: sess-abc\nproject: /work\n---\n")
        assert find_session_file(tmp_path, "sess-xyz") is None

    def test_returns_none_for_empty_dir(self, tmp_path):
        assert find_session_file(tmp_path, "sess-abc") is None


# ── find_project_sessions ─────────────────────────────────────────────────────


class TestFindProjectSessions:
    def _make_session(self, base, date_str, slug, project):
        d = base / date_str
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{slug}.md"
        f.write_text(
            f"---\nsession: sess-{slug}\nproject: {project}\ndate: {date_str}\n---\n"
        )
        return f

    def test_returns_matching_sessions(self, tmp_path):
        self._make_session(tmp_path, "2026-03-01", "fix-bug", "/project/a")
        self._make_session(tmp_path, "2026-03-02", "add-feature", "/project/b")
        result = find_project_sessions(tmp_path, Path("/project/a"))
        assert len(result) == 1
        assert result[0].stem == "fix-bug"

    def test_returns_empty_for_no_match(self, tmp_path):
        self._make_session(tmp_path, "2026-03-01", "fix-bug", "/project/a")
        result = find_project_sessions(tmp_path, Path("/project/c"))
        assert result == []

    def test_sorted_newest_first(self, tmp_path):
        import time
        self._make_session(tmp_path, "2026-03-01", "old-session", "/project/a")
        time.sleep(0.02)
        self._make_session(tmp_path, "2026-03-02", "new-session", "/project/a")
        result = find_project_sessions(tmp_path, Path("/project/a"))
        assert result[0].stem == "new-session"
        assert result[1].stem == "old-session"

    def test_empty_dir(self, tmp_path):
        assert find_project_sessions(tmp_path, Path("/project/a")) == []


# ── generate_slug ──────────────────────────────────────────────────────────────


class TestGenerateSlug:
    def test_returns_sanitized_slug(self):
        with patch("cl_tool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Fix Auth Bug\n")
            result = generate_slug("Fix the auth bug", Path("/project"))
            assert result == "fix-auth-bug"

    def test_falls_back_on_failure(self):
        with patch("cl_tool.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = generate_slug("Fix bug", Path("/project"))
            assert result.startswith("session-")

    def test_falls_back_on_timeout(self):
        import subprocess
        with patch("cl_tool.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 30)
            result = generate_slug("Fix bug", Path("/project"))
            assert result.startswith("session-")

    def test_falls_back_on_empty_output(self):
        with patch("cl_tool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = generate_slug("Fix bug", Path("/project"))
            assert result.startswith("session-")

    def test_uses_custom_claude_bin(self):
        with patch("cl_tool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="my-slug\n")
            generate_slug("prompt", Path("/p"), claude_bin="/usr/bin/my-claude")
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "/usr/bin/my-claude"


# ── run_pipeline KeyboardInterrupt ────────────────────────────────────────────


class TestRunPipelineKeyboardInterrupt:
    """Verify that Ctrl+C during run_pipeline terminates both subprocesses."""

    def test_keyboard_interrupt_terminates_processes(self, tmp_path):
        json_path = tmp_path / "out.json"
        md_path = tmp_path / "out.md"

        original_join = threading.Thread.join

        def interrupt_on_join(self_thread, *args, **kwargs):
            raise KeyboardInterrupt

        with (
            patch("cl_tool.subprocess.Popen") as mock_popen_cls,
            patch.object(threading.Thread, "join", interrupt_on_join),
        ):
            mock_claude = MagicMock()
            mock_claude.stdin = MagicMock()
            mock_claude.stdout = iter([])
            mock_claude.__enter__ = MagicMock(return_value=mock_claude)
            mock_claude.__exit__ = MagicMock(return_value=False)

            mock_pmr = MagicMock()
            mock_pmr.stdin = MagicMock()
            mock_pmr.__enter__ = MagicMock(return_value=mock_pmr)
            mock_pmr.__exit__ = MagicMock(return_value=False)

            mock_popen_cls.side_effect = [mock_claude, mock_pmr]

            with pytest.raises(KeyboardInterrupt):
                run_pipeline("test", [], json_path, md_path)

            mock_claude.terminate.assert_called_once()
            mock_pmr.terminate.assert_called_once()
            mock_claude.wait.assert_called_once()
            mock_pmr.wait.assert_called_once()

    def test_tee_handles_broken_pipe(self, tmp_path):
        """_tee() catches BrokenPipeError when pmr exits early."""
        json_path = tmp_path / "out.json"
        md_path = tmp_path / "out.md"

        with (
            patch("cl_tool.subprocess.Popen") as mock_popen_cls,
        ):
            mock_claude = MagicMock()
            mock_claude.stdin = MagicMock()
            # Simulate claude producing output lines.
            mock_claude.stdout = iter(['{"type":"system"}\n', '{"type":"result"}\n'])
            mock_claude.__enter__ = MagicMock(return_value=mock_claude)
            mock_claude.__exit__ = MagicMock(return_value=False)

            mock_pmr = MagicMock()
            mock_pmr.stdin = MagicMock()
            # pmr stdin write raises BrokenPipeError on first write.
            mock_pmr.stdin.write.side_effect = BrokenPipeError("broken pipe")
            mock_pmr.returncode = 0
            mock_pmr.__enter__ = MagicMock(return_value=mock_pmr)
            mock_pmr.__exit__ = MagicMock(return_value=False)

            mock_popen_cls.side_effect = [mock_claude, mock_pmr]

            # Should not raise — _tee catches BrokenPipeError.
            result = run_pipeline("test", [], json_path, md_path)
            assert result == 0


# ── main() KeyboardInterrupt ──────────────────────────────────────────────────


class TestMainKeyboardInterrupt:
    def test_main_returns_130_on_ctrl_c(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr("cl_tool.Path.cwd", staticmethod(lambda: tmp_path))

        with (
            patch("cl_tool.run_pipeline", side_effect=KeyboardInterrupt),
            patch("cl_tool.read_piped_stdin", return_value=None),
            patch("cl_tool.edit_prompt", return_value="test prompt"),
            patch("cl_tool.setup_git_excludes"),
        ):
            rc = main([])
            assert rc == 130
