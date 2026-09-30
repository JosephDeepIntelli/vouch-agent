"""CLI surface for the native lifecycle: reconcile, export-run, run-detach."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vouch_agent.cli.main import app

runner = CliRunner()

LEFT = "sku,price\nA-1,10\nA-2,20\n"
RIGHT = "sku,price\nA-1,10\nA-2,25\n"


def _project(tmp_path: Path) -> Path:
    result = runner.invoke(
        app,
        ["init", "--project", str(tmp_path), "--workflow", "W-C3",
         "--owners", "acceptance-owner=ana", "--owners", "release-owner=roger", "--cap", "2.0"],
    )
    assert result.exit_code == 0, result.output
    return tmp_path


def test_reconcile_command_reports_and_exports(tmp_path: Path) -> None:
    project = _project(tmp_path)
    left = tmp_path / "left.csv"
    left.write_text(LEFT, encoding="utf-8")
    right = tmp_path / "right.csv"
    right.write_text(RIGHT, encoding="utf-8")
    out = tmp_path / "export"
    result = runner.invoke(
        app,
        ["reconcile", "--project", str(project), "--left", str(left), "--right", str(right),
         "--join-key", "sku", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "1 changed" in result.output and "matched" in result.output
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "vouch-native-run-export"
    assert manifest["notDoneItems"]


def test_reconcile_validates_join_key(tmp_path: Path) -> None:
    project = _project(tmp_path)
    left = tmp_path / "left.csv"
    left.write_text(LEFT, encoding="utf-8")
    right = tmp_path / "right.csv"
    right.write_text(RIGHT, encoding="utf-8")
    result = runner.invoke(
        app,
        ["reconcile", "--project", str(project), "--left", str(left), "--right", str(right),
         "--join-key", "nope"],
    )
    assert result.exit_code != 0
    assert "join key" in result.output.lower()


def test_export_run_command_round_trips(tmp_path: Path) -> None:
    project = _project(tmp_path)
    left = tmp_path / "l.csv"
    left.write_text(LEFT, encoding="utf-8")
    right = tmp_path / "r.csv"
    right.write_text(RIGHT, encoding="utf-8")
    reconcile = runner.invoke(
        app,
        ["reconcile", "--project", str(project), "--left", str(left), "--right", str(right),
         "--join-key", "sku"],
    )
    assert reconcile.exit_code == 0, reconcile.output
    run_id = reconcile.output.split("run: ")[1].split()[0]
    out2 = tmp_path / "export2"
    export = runner.invoke(
        app, ["export-run", "--project", str(project), run_id, "--out", str(out2)]
    )
    assert export.exit_code == 0, export.output
    assert (out2 / "manifest.json").exists()
