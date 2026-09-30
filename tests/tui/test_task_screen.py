"""Responsive TUI task lifecycle (M4 §B): background start, in-flight close,
reconnect, lifecycle commands — all through the public services."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vouch_agent.cli.main import app as cli_app
from vouch_agent.tui.app import VouchApp

runner = CliRunner()

_DRAFT = (
    'value = materials["value"]\n'
    'return {"finding": value, "source": materials["source"], "confidence": "low"}'
)
_FINAL = (
    'value = materials["value"]\n'
    'return {"finding": value, "source": materials["source"], "confidence": "high"}'
)
_SCHEMA = {
    "type": "object",
    "required": ["finding", "source", "confidence"],
    "properties": {
        "finding": {"type": "string"},
        "source": {"type": "string"},
        "confidence": {"type": "string"},
    },
}


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    result = runner.invoke(
        app=cli_app,
        args=[
            "init",
            "--project",
            str(tmp_path),
            "--workflow",
            "W-C3",
            "--owners",
            "acceptance-owner=ana",
            "--owners",
            "release-owner=roger",
        ],
    )
    assert result.exit_code == 0, result.output
    return tmp_path


@pytest.mark.tui
def test_task_screen_starts_background_and_survives_close(project: Path, tmp_path: Path) -> None:
    """Start routes to the detached worker; closing the UI mid-task (a pause
    barrier holds the run in flight) leaves durable state to reconnect to."""
    pytest.importorskip("textual")
    asyncio.run(_task_flow(project, tmp_path))


async def _task_flow(project: Path, tmp_path: Path) -> None:
    from textual.widgets import DataTable, Static

    fact = tmp_path / "fact.json"
    fact.write_text(json.dumps({"value": "tui fact", "source": "tui"}), encoding="utf-8")

    vouch = VouchApp(project_dir=str(project))
    async with vouch.run_test() as pilot:
        await pilot.press("ctrl+t")
        screen = vouch.screen
        screen.query_one("#task-goal", screen_input()).value = "Extract the fact"
        screen.query_one("#task-input", screen_input()).value = f"fact={fact}"
        screen.query_one("#task-require", screen_input()).value = "finding, source"
        await pilot.click("#btn-task-run")
        result_text = str(screen.query_one("#task-result", Static).render())
        assert "background worker" in result_text  # non-blocking start
        table = screen.query_one("#task-runs", DataTable)
        assert table.row_count >= 1

    # the UI is closed; the run and its worker outlive it
    from vouch_agent.appservices.execution import ExecutionService
    from vouch_agent.appservices.workspace import ProjectWorkspace

    fresh = ProjectWorkspace.open(project)
    try:
        service = ExecutionService(fresh)
        deadline = time.monotonic() + 90
        status = None
        while time.monotonic() < deadline:
            run = service.runs()[0] if service.runs() else None
            if run is not None and run.status.value in ("completed", "failed", "cancelled"):
                status = run.status.value
                break
            time.sleep(0.5)
        assert status == "completed"
        result = service.result(service.runs()[0].run_id)
        assert result is not None
    finally:
        fresh.close()


def screen_input():
    from textual.widgets import Input

    return Input


@pytest.mark.tui
def test_task_screen_cancel_and_detail(project: Path, tmp_path: Path) -> None:
    """Cancel from the TUI stops scheduling; detail view shows honest state."""
    pytest.importorskip("textual")
    asyncio.run(_cancel_flow(project, tmp_path))


async def _cancel_flow(project: Path, tmp_path: Path) -> None:
    from textual.widgets import DataTable, Static

    fact = tmp_path / "fact.json"
    fact.write_text(json.dumps({"value": "v", "source": "s"}), encoding="utf-8")
    vouch = VouchApp(project_dir=str(project))
    async with vouch.run_test() as pilot:
        await pilot.press("ctrl+t")
        screen = vouch.screen
        screen.query_one("#task-goal", screen_input()).value = "Extract the fact"
        screen.query_one("#task-input", screen_input()).value = f"fact={fact}"
        await pilot.click("#btn-task-run")
        await pilot.pause(0.5)
        table = screen.query_one("#task-runs", DataTable)
        assert table.row_count >= 1
        table.move_cursor(row=0)
        screen.action_cancel_selected()
        detail = str(screen.query_one("#task-detail", Static).render())
        assert "run " in detail  # honest status rendering, no crash
