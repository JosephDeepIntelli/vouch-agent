"""Shared CLI test helpers (imported by tests in this directory).

pytest's default import mode puts this directory on sys.path for the tests
that live here, so a plain ``from helpers import ...`` works without making
``tests`` a package.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner, Result

from vouch_agent.cli.main import app

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "choose"

RUNNER = CliRunner()


def invoke(*args: str | Path, input_text: str | None = None) -> Result:
    return RUNNER.invoke(app, [str(arg) for arg in args], input=input_text)


def out(result: Result) -> str:
    """stdout + stderr combined, so assertions never depend on stream choice."""
    return result.stdout + (result.stderr or "")


def init_project(
    tmp_path: Path,
    *,
    workflow: str = "W-C3",
    cap: float = 5.0,
    change_types: Sequence[str] = (),
) -> Path:
    project = tmp_path / "proj"
    args = [
        "init",
        "--project",
        str(project),
        "--workflow",
        workflow,
        "--cap",
        str(cap),
        "--owners",
        "acceptance-owner=ana",
        "--owners",
        "release-owner=roger",
    ]
    for ctype in change_types:
        args.extend(["--change-type", ctype])
    result = invoke(*args)
    assert result.exit_code == 0, out(result)
    return project


def baseline(project: Path, *, version: str = "v0", ref: str = "git:abc0") -> Result:
    return invoke(
        "baseline",
        "--project",
        str(project),
        "--version",
        version,
        "--source-ref",
        ref,
    )


def fixture_pack(project: Path, *, dev: int = 1, selection: int = 0, final: int = 1) -> Result:
    return invoke(
        "pack",
        "--project",
        str(project),
        "--from-fixture",
        str(FIXTURES),
        "--workflow",
        "W-C3",
        "--dev",
        str(dev),
        "--selection",
        str(selection),
        "--final",
        str(final),
    )


def selection_pack(project: Path) -> Result:
    """Re-import the synthetic pack WITH a selection-validation case.

    W-C3 has two fixture scenarios, so the selection/final split pair is the
    promotion path: a development-only evaluation can no longer advance a
    candidate to acceptance-eligible (review A5).
    """
    return fixture_pack(project, dev=0, selection=1, final=1)


def evaluate_selection(project: Path, candidate: str = "cand-1", **kwargs: str) -> Result:
    args = [
        "evaluate",
        "--project",
        str(project),
        "--candidate",
        candidate,
        "--pack",
        "w-c3-synthetic",
        "--split",
        "selection-validation",
    ]
    for key, value in kwargs.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return invoke(*args)


DELTA_TEXT = "+ require a citation for every factual claim\n"


def propose_sealed(project: Path, candidate_id: str = "cand-1") -> Result:
    return invoke(
        "propose",
        "--project",
        str(project),
        "--delta-file",
        "-",
        "--type",
        "prompt-delta",
        "--rationale",
        "synthetic cluster motivation",
        "--id",
        candidate_id,
        "--seal",
        input_text=DELTA_TEXT,
    )
