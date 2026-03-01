"""Shared fixtures for cl tests."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CL_TOOL = Path(__file__).parent.parent / "cl_tool.py"

# Default mock prompt used by the mock editor unless overridden.
DEFAULT_MOCK_PROMPT = "What is 2+2?"


@pytest.fixture()
def cl_env(tmp_path):
    """Isolated environment with mock claude, editor, and print-my-ride binaries.

    Yields a dict with:
      work_dir  — cwd for the cl subprocess (contains .cl_history after run)
      home_dir  — HOME override (isolates git config changes)
      env       — environment dict to pass to subprocess.run
      bin_dir   — directory containing mock binaries
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    for cmd, src in [
        ("claude", "mock_claude.py"),
        ("editor", "mock_editor.py"),
        ("print-my-ride", "mock_pmr.py"),
    ]:
        dst = bin_dir / cmd
        shutil.copy(FIXTURES_DIR / src, dst)
        # Re-write shebang to use the active interpreter so the scripts work
        # inside whatever venv pytest is running in.
        text = dst.read_text()
        lines = text.splitlines(keepends=True)
        lines[0] = f"#!{sys.executable}\n"
        dst.write_text("".join(lines))
        dst.chmod(0o755)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["EDITOR"] = str(bin_dir / "editor")
    env["HOME"] = str(home_dir)
    env["MOCK_EDITOR_PROMPT"] = DEFAULT_MOCK_PROMPT

    yield {
        "work_dir": work_dir,
        "home_dir": home_dir,
        "bin_dir": bin_dir,
        "env": env,
    }
