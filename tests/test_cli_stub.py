"""CLI stub smoke — the full command set lands with the CLI/TUI package."""

from __future__ import annotations

import subprocess
import sys


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vouch_agent.cli.main", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_version_reports_package_and_pinned_jaz() -> None:
    result = _run("version")
    assert result.returncode == 0, result.stderr
    assert "vouch-agent" in result.stdout
    assert "jaz-lang 0.2.0a4" in result.stdout


def test_modes_list_distinguishes_run_modes() -> None:
    result = _run("modes")
    assert result.returncode == 0, result.stderr
    assert "fixture: live-calls=False" in result.stdout
    assert "authorized-live: live-calls=True" in result.stdout
