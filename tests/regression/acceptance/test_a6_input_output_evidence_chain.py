"""A6 regressions: input/output evidence chain and export closure.

Converted from the coordinator reproduction `review-acceptance-20260928`
(defect 7 plus the evidence-export follow-ups). CLI scenarios use the public
`vouch` commands; the import verification is also probed at the service level.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from helpers import baseline, init_project, invoke, out, propose_sealed

from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.common import digest_bytes


def _selection_pack(project: Path) -> None:
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


def _ready(tmp_path: Path) -> Path:
    project = init_project(tmp_path)
    assert baseline(project).exit_code == 0
    _selection_pack(project)
    assert propose_sealed(project).exit_code == 0
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
    return project


def _pack_input_digests(project: Path) -> dict[str, str]:
    workspace = ProjectWorkspace.open(project)
    try:
        data = workspace.store.load("task-pack", "w-c3-synthetic")
        assert data is not None
        return {case["caseId"]: case["inputDigest"] for case in data["cases"]}
    finally:
        workspace.close()


def _corrupt_input(project: Path, case_id: str) -> None:
    digest = _pack_input_digests(project)[case_id]
    path = project / ".vouch" / "artifacts" / digest.removeprefix("sha256:")
    assert path.is_file()
    path.write_bytes(b"tampered")  # this temp workspace's synthetic input only


# -- defect 7: input tamper must fail BEFORE any adapter work ------------------------------


def test_corrupted_stored_case_input_rejected_before_adapter_work(tmp_path: Path) -> None:
    """Reproduction 7 (inverted): corrupting the stored synthetic case-input
    bytes must make stored-pack evaluation fail with a digest mismatch before
    any adapter executes."""
    project = _ready(tmp_path)
    _corrupt_input(project, "compare-normal-zh-cn")
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
    combined = out(result)
    assert result.exit_code != 0, combined
    assert "[vouch/digest-mismatch]" in combined
    # nothing executed and nothing was journaled as spend for a new run
    workspace = ProjectWorkspace.open(project)
    try:
        runs = workspace.store.list_ids("evaluation")
        # only the earlier selection run exists; no new run was started
        assert len(runs) == 1
    finally:
        workspace.close()


def test_corrupted_final_acceptance_input_rejected_before_adapter_work(tmp_path: Path) -> None:
    project = _ready(tmp_path)
    _corrupt_input(project, "compare-adverse-blocked-partial-en-us")
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
        "ana",
    )
    combined = out(result)
    assert result.exit_code != 0, combined
    assert "[vouch/digest-mismatch]" in combined
    workspace = ProjectWorkspace.open(project)
    try:
        data = workspace.store.load("candidate", "cand-1")
        assert data is not None
        assert data["state"] == "evaluated"  # never accepted on tampered input
    finally:
        workspace.close()


def test_import_pack_file_verifies_input_bytes_not_existence_only(tmp_path: Path) -> None:
    """A pack whose named input artifact exists but no longer digests to its
    name must be refused at import (previously only existence was checked)."""
    from helpers import FIXTURES

    project = init_project(tmp_path)
    assert baseline(project).exit_code == 0
    pack_file = tmp_path / "sel-pack.json"
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
        "--out",
        str(pack_file),
    )
    assert result.exit_code == 0, out(result)
    assert propose_sealed(project).exit_code == 0
    workspace = ProjectWorkspace.open(project)
    try:
        first_case = json.loads(pack_file.read_text())["cases"][0]
        digest = first_case["inputDigest"]
        (project / ".vouch" / "artifacts" / digest.removeprefix("sha256:")).write_bytes(b"tampered")
    finally:
        workspace.close()
    result = invoke(
        "evaluate",
        "--project",
        str(project),
        "--candidate",
        "cand-1",
        "--pack",
        str(pack_file),
        "--split",
        "selection-validation",
    )
    combined = out(result)
    assert result.exit_code != 0, combined
    assert "digest-mismatch" in combined


# -- evidence export closes the loop --------------------------------------------------------


def test_export_closes_inputs_outputs_and_run_scoped_costs(tmp_path: Path) -> None:
    """The exported package must carry the verified inputs/outputs needed to
    review the decision, and ONLY this run's cost lineage."""
    project = _ready(tmp_path)
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

    workspace = ProjectWorkspace.open(project)
    try:
        run_ids = sorted(workspace.store.list_ids("evaluation"))
        assert len(run_ids) == 2  # selection run + final-acceptance run
        by_split = {}
        for run_id in run_ids:
            data = workspace.store.load("evaluation", run_id)
            assert data is not None
            by_split[data["split"]] = (run_id, data)
        final_run_id, _final_data = by_split["final-acceptance"]
        _selection_run_id, selection_data = by_split["selection-validation"]
        selection_attempt_ids = {a["attemptId"] for a in selection_data["attempts"]}
    finally:
        workspace.close()

    destination = tmp_path / "export"
    result = invoke(
        "export",
        "--project",
        str(project),
        "--run",
        final_run_id,
        "--out",
        str(destination),
    )
    assert result.exit_code == 0, out(result)

    inputs = json.loads((destination / "evidence-inputs.json").read_text())
    assert inputs["entries"], "no case inputs closed into the package"
    for entry in inputs["entries"]:
        assert entry["available"] is True, entry
        payload = base64.b64decode(entry["payloadB64"])
        assert digest_bytes(payload) == entry["digest"]
    # the final case's input bytes really are the pack's stored bytes
    digests = _pack_input_digests(project)
    assert {e["digest"] for e in inputs["entries"]} == {
        digests["compare-adverse-blocked-partial-en-us"]
    }

    outputs = json.loads((destination / "evidence-outputs.json").read_text())
    assert outputs["entries"]
    for entry in outputs["entries"]:
        assert entry["available"] is True, entry
        payload = base64.b64decode(entry["payloadB64"])
        assert digest_bytes(payload) == entry["digest"]

    costs = json.loads((destination / "cost-journal.json").read_text())["entries"]
    subjects = {c["subject"] for c in costs}
    assert subjects  # run-level lineage is present
    assert not (subjects & selection_attempt_ids), "selection-run costs leaked into the export"

    # the package verifies standalone: no undeclared sibling files needed
    from vouch_agent.export import verify_package

    package = verify_package(destination)
    assert package.run_id == final_run_id


def test_export_declares_unavailable_evidence_explicitly(tmp_path: Path) -> None:
    """When a sealed output artifact has been lost, the export must say so
    explicitly instead of failing or silently omitting the attempt."""
    project = _ready(tmp_path)
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

    workspace = ProjectWorkspace.open(project)
    try:
        final_run = next(
            run_id
            for run_id in workspace.store.list_ids("evaluation")
            if (workspace.store.load("evaluation", run_id) or {}).get("split") == "final-acceptance"
        )
        data = workspace.store.load("evaluation", final_run)
        assert data is not None
        output_digests = [a["outputDigest"] for a in data["attempts"] if a.get("outputDigest")]
        assert output_digests
        for digest in output_digests:
            (project / ".vouch" / "artifacts" / digest.removeprefix("sha256:")).unlink()
    finally:
        workspace.close()

    destination = tmp_path / "export"
    result = invoke(
        "export",
        "--project",
        str(project),
        "--run",
        final_run,
        "--out",
        str(destination),
    )
    assert result.exit_code == 0, out(result)
    outputs = json.loads((destination / "evidence-outputs.json").read_text())
    assert outputs["entries"]
    assert all(entry["available"] is False for entry in outputs["entries"])
    assert all(entry.get("unavailableReason") for entry in outputs["entries"])
