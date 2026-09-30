"""A5 regressions: independent acceptance, owner validation, drift chain.

Converted from the coordinator reproduction `review-acceptance-20260928`
(defects 1 and 2 plus the selection-gate and final-repeats follow-ups). Every
scenario goes through the public CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import baseline, init_project, invoke, out, propose_sealed

from vouch_agent.appservices.workspace import ProjectWorkspace


def _selection_pack(project: Path) -> None:
    """Import a pack with selection-validation AND final-acceptance cases."""
    from helpers import FIXTURES

    result = invoke(
        "pack",
        "--project",
        str(project),
        "--from-fixture",
        str(FIXTURES),
        "--workflow",
        "W-C3",
        "--dev",
        "0",
        "--selection",
        "1",
        "--final",
        "1",
    )
    assert result.exit_code == 0, out(result)


def _ready(tmp_path: Path, *, cap: float = 5.0, repeats: int = 1) -> Path:
    project = init_project(tmp_path, cap=cap)
    assert baseline(project).exit_code == 0
    _selection_pack(project)
    assert propose_sealed(project).exit_code == 0
    return project


def _run_selection_evaluation(project: Path) -> None:
    result = invoke(
        "evaluate",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--split",
        "selection-validation",
    )
    assert result.exit_code == 0, out(result)
    assert "candidate advanced to 'evaluated'" in result.stdout


def _candidate_state(project: Path) -> str:
    workspace = ProjectWorkspace.open(project)
    try:
        data = workspace.store.load("candidate", "cand-1")
        assert data is not None
        return str(data["state"])
    finally:
        workspace.close()


# -- defect 1: approval drift ----------------------------------------------------------


def test_approve_refuses_after_baseline_and_rubric_drift(tmp_path: Path) -> None:
    """Reproduction 1 (inverted): evaluate -> accept -> change baseline/rubric
    -> approve must REFUSE instead of approving stale evidence."""
    project = _ready(tmp_path)
    _run_selection_evaluation(project)
    result = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(tmp_path / "evidence"),
        "--owner",
        "ana",
    )
    assert result.exit_code == 0, out(result)
    assert _candidate_state(project) == "accepted"

    drifted = invoke(
        "baseline",
        "--project",
        str(project),
        "--version",
        "v1",
        "--source-ref",
        "git:new",
        "--min-improvement",
        "999",
    )
    assert drifted.exit_code == 0, out(drifted)

    approve = invoke("approve", "--project", str(project), "--candidate", "cand-1")
    combined = out(approve)
    assert approve.exit_code != 0, combined
    assert "approval-invalidated" in combined
    assert _candidate_state(project) == "accepted"  # not advanced

    release = invoke(
        "release",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--deployed-version",
        "v1",
        "--deployed-by",
        "nobody",
    )
    assert release.exit_code != 0, out(release)
    assert _candidate_state(project) == "accepted"


def test_release_independently_rechecks_after_post_approval_drift(tmp_path: Path) -> None:
    """The drift chain must ALSO break at release when the drift happens after
    a legitimate approve: release re-resolves current trusted state itself."""
    project = _ready(tmp_path)
    _run_selection_evaluation(project)
    assert (
        invoke(
            "accept",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
            "--out",
            str(tmp_path / "evidence"),
            "--owner",
            "ana",
        ).exit_code
        == 0
    )
    assert invoke("approve", "--project", str(project), "--candidate", "cand-1").exit_code == 0

    drifted = invoke(
        "baseline",
        "--project",
        str(project),
        "--version",
        "v2",
        "--source-ref",
        "git:newer",
        "--min-improvement",
        "999",
    )
    assert drifted.exit_code == 0, out(drifted)

    release = invoke(
        "release",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--deployed-version",
        "v2",
        "--deployed-by",
        "roger",
    )
    combined = out(release)
    assert release.exit_code != 0, combined
    assert "approval-invalidated" in combined
    assert _candidate_state(project) == "approved"  # not released


# -- defect 2: self-acceptance / owner validation ----------------------------------------


def test_accept_refuses_when_owner_is_the_candidate_proposer(tmp_path: Path) -> None:
    """Reproduction 2 (inverted): `accept --owner proposer-agent` for a
    candidate proposed by `proposer-agent` must fail, not accept."""
    project = _ready(tmp_path)
    _run_selection_evaluation(project)
    result = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(tmp_path / "ev"),
        "--owner",
        "proposer-agent",
    )
    combined = out(result)
    assert result.exit_code != 0, combined
    assert "proposer" in combined
    assert _candidate_state(project) == "evaluated"


def test_accept_refuses_owner_who_is_not_the_configured_acceptance_owner(
    tmp_path: Path,
) -> None:
    project = _ready(tmp_path)
    _run_selection_evaluation(project)
    result = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(tmp_path / "ev"),
        "--owner",
        "someone-else",
    )
    combined = out(result)
    assert result.exit_code != 0, combined
    assert "acceptance owner" in combined
    assert _candidate_state(project) == "evaluated"


def test_accept_fails_fast_on_bad_owner_without_spending_final_budget(tmp_path: Path) -> None:
    """Owner validation happens BEFORE the final-acceptance run executes."""
    project = _ready(tmp_path, cap=1.0)
    _run_selection_evaluation(project)
    runs_before = _run_ids(project)
    result = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(tmp_path / "ev"),
        "--owner",
        "proposer-agent",
    )
    assert result.exit_code != 0, out(result)
    assert _run_ids(project) == runs_before  # no final-acceptance run was executed


# -- selection gate follow-up -------------------------------------------------------------


def test_development_only_evaluation_cannot_reach_acceptance(tmp_path: Path) -> None:
    """A development-split acceptance verdict must NOT promote the candidate
    to acceptance-eligible; only a qualifying selection-validation run does."""
    project = init_project(tmp_path)
    assert baseline(project).exit_code == 0
    from helpers import fixture_pack

    assert fixture_pack(project).exit_code == 0  # dev=1, final=1, no selection case
    assert propose_sealed(project).exit_code == 0
    result = invoke(
        "evaluate",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
    )
    assert result.exit_code == 0, out(result)
    assert "verdict: accepted" in result.stdout
    assert "advanced to 'evaluated'" not in result.stdout
    assert _candidate_state(project) == "sealed"

    accept = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(tmp_path / "ev"),
        "--owner",
        "ana",
    )
    combined = out(accept)
    assert accept.exit_code != 0, combined
    assert "selection" in combined
    assert _candidate_state(project) == "sealed"


# -- final acceptance honors rubric repeats -------------------------------------------------


def test_final_acceptance_honors_frozen_rubric_repeats(tmp_path: Path) -> None:
    """A rubric frozen with repeats=2 must drive TWO repeats per side at final
    acceptance (previously the final split always ran a single pass)."""
    project = init_project(tmp_path)
    result = invoke(
        "baseline",
        "--project",
        str(project),
        "--version",
        "v0",
        "--source-ref",
        "git:abc0",
        "--repeats",
        "2",
    )
    assert result.exit_code == 0, out(result)
    _selection_pack(project)
    assert propose_sealed(project).exit_code == 0
    _run_selection_evaluation(project)

    evidence = tmp_path / "evidence"
    result = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(evidence),
        "--owner",
        "ana",
    )
    assert result.exit_code == 0, out(result)
    attempts = json.loads((evidence / "attempts.json").read_text())["attempts"]
    # 1 final-acceptance case x 2 sides x 2 repeats
    assert len(attempts) == 4, attempts
    assert _candidate_state(project) == "accepted"


# -- helpers ---------------------------------------------------------------------------------


def _run_ids(project: Path) -> list[str]:
    workspace = ProjectWorkspace.open(project)
    try:
        return sorted(workspace.store.list_ids("evaluation"))
    finally:
        workspace.close()
