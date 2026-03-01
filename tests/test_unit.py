"""Unit tests for cl_tool helper functions.

No subprocesses are spawned here — each function is tested in isolation
with mocks where external I/O is required.
"""

from __future__ import annotations

import configparser
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from cl_tool import (
    _GIT_EXCLUDE_ENTRY,
    _read_global_excludes_file,
    append_to_history,
    build_stdin_block,
    extract_session_id,
    find_sessions,
    parse_args,
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

    def test_continue_is_forwarded_to_claude(self):
        _, claude_args = parse_args(["-c"])
        assert "--continue" in claude_args

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

    def test_continue_not_in_claude_args_twice(self):
        # --continue should appear exactly once even if -c is passed.
        _, claude_args = parse_args(["-c"])
        assert claude_args.count("--continue") == 1

    def test_mixed_cl_and_claude_args(self):
        ns, claude_args = parse_args(["-c", "--model", "claude-3", "--verbose"])
        assert ns.continue_session is True
        assert "--model" in claude_args
        assert "claude-3" in claude_args
        assert "--verbose" in claude_args
        assert "--continue" in claude_args

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
