"""Stage C CLI tests: `vouch run` / `runs` / `run-status` / `run-cancel`.

Headless (CliRunner), offline, deterministic. The isolated default path
spawns the real rlimit-bounded worker subprocess per step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vouch_agent.cli.main import app

runner = CliRunner()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    result = runner.invoke(
        app,
        [
            "init",
            "--project",
            str(tmp_path),
            "--workflow",
            "W-C3",
            "--owners",
            "acceptance-owner=ana",
            "--owners",
            "release-owner=roger",
            "--cap",
            "2.0",
        ],
    )
    assert result.exit_code == 0, result.output
    return tmp_path


def _fact(tmp_path: Path, value: str, source: str = "sample") -> Path:
    path = tmp_path / "fact.json"
    path.write_text(json.dumps({"value": value, "source": source}), encoding="utf-8")
    return path


def test_run_completes_with_machine_checked_conditions(project: Path, tmp_path: Path) -> None:
    fact = _fact(tmp_path, "迪普智选 sample fact")
    result = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(project),
            "--goal",
            "Extract the fact",
            "--input",
            f"fact={fact}",
            "--require-field",
            "finding",
            "--require-field",
            "source",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "status: completed" in result.output
    assert "fixture providers, deterministic; not a live model" in result.output
    assert "rlimit-bounded worker process, same OS user" in result.output
    assert "artifact final:" in result.output


def test_run_fails_closed_without_completion_conditions(project: Path, tmp_path: Path) -> None:
    fact = _fact(tmp_path, "some fact")
    result = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(project),
            "--goal",
            "Extract the fact",
            "--input",
            f"fact={fact}",
        ],
    )
    assert result.exit_code == 1
    assert "no --require-field/--expect-text" in result.output
    assert "status: failed" in result.output


def test_run_tracks_the_supplied_material(project: Path, tmp_path: Path) -> None:
    """Same command shape, different material -> different delivered finding
    (the scoped-input proof, through the public CLI)."""
    outputs = []
    for value in ("fact alpha", "fact beta"):
        fact = _fact(tmp_path, value)
        result = runner.invoke(
            app,
            [
                "run",
                "--project",
                str(project),
                "--goal",
                "Extract the fact",
                "--input",
                f"fact={fact}",
                "--require-field",
                "finding",
            ],
        )
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
    assert "alpha" in outputs[0] and "beta" not in outputs[0]
    assert "beta" in outputs[1] and "alpha" not in outputs[1]


def test_runs_listing_and_run_status(project: Path, tmp_path: Path) -> None:
    fact = _fact(tmp_path, "listed fact")
    run_result = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(project),
            "--goal",
            "Extract",
            "--input",
            f"fact={fact}",
            "--require-field",
            "finding",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    run_id = run_result.output.split("run: ")[1].split()[0]

    listing = runner.invoke(app, ["runs", "--project", str(project)])
    assert listing.exit_code == 0, listing.output
    assert run_id in listing.output
    assert "completed" in listing.output

    status_result = runner.invoke(app, ["run-status", "--project", str(project), run_id])
    assert status_result.exit_code == 0, status_result.output
    assert "recovery:" in status_result.output
    assert "deliverable: True" in status_result.output


def test_run_status_unknown_run_fails(project: Path) -> None:
    result = runner.invoke(app, ["run-status", "--project", str(project), "run_missing"])
    assert result.exit_code == 1
    assert "unknown run" in result.output


def test_run_requires_inputs(project: Path) -> None:
    result = runner.invoke(
        app, ["run", "--project", str(project), "--goal", "x", "--require-field", "a"]
    )
    assert result.exit_code != 0
    assert "at least one --input" in result.output


def test_inline_flag_states_its_narrower_boundary(project: Path, tmp_path: Path) -> None:
    fact = _fact(tmp_path, "inline fact")
    result = runner.invoke(
        app,
        [
            "run",
            "--project",
            str(project),
            "--goal",
            "Extract",
            "--input",
            f"fact={fact}",
            "--require-field",
            "finding",
            "--inline",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "in-process (trusted sample scripts only)" in result.output
