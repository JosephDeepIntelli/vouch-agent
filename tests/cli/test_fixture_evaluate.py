"""End-to-end: `vouch evaluate --adapter fixture` drives the REAL subprocess.

The fixture adapter is spawned via ProcessAdapterClient +
fixture_adapter_command() — every describe/prepare/execute/collect/cleanup is a
protocol-v1 frame across a real process boundary. These tests assert the
metering and evidence digests survive that round trip, plus the honest
zero-delta semantics of synthetic replay.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import (
    DELTA_TEXT,
    FIXTURES,
    baseline,
    evaluate_selection,
    init_project,
    invoke,
    out,
    selection_pack,
)

from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.journal import EventKind
from vouch_agent.export import verify_package


def test_fixture_subprocess_evaluation_metering_and_evidence(
    tmp_path: Path,
) -> None:
    project = init_project(tmp_path)
    assert baseline(project).exit_code == 0
    result = invoke(
        "pack",
        "--project",
        str(project),
        "--from-fixture",
        str(FIXTURES),
        "--workflow",
        "W-C3",
        "--dev",
        "1",
        "--final",
        "1",
    )
    assert result.exit_code == 0, out(result)
    result = invoke(
        "propose",
        "--project",
        str(project),
        "--delta-file",
        "-",
        "--type",
        "prompt-delta",
        "--rationale",
        "fixture subprocess check",
        "--id",
        "cand-fx",
        "--seal",
        input_text=DELTA_TEXT,
    )
    assert result.exit_code == 0, out(result)

    result = invoke(
        "evaluate",
        "--project",
        str(project),
        "--candidate",
        "cand-fx",
        "--pack",
        "w-c3-synthetic",
        "--adapter",
        "fixture",
    )
    assert result.exit_code == 0, out(result)
    stdout = result.stdout
    assert "adapter=fixture-choose-v1+scenario-client@1" in stdout
    assert "mode: fixture" in stdout
    assert "non-inferiority on synthetic data" in stdout
    # Synthetic replay: both sides run the same scenario -> zero deltas.
    assert "(delta +0.0000" in stdout

    workspace = ProjectWorkspace.open(project)
    try:
        run_ids = workspace.store.list_ids("evaluation")
        assert len(run_ids) == 1
        run_data = workspace.store.load("evaluation", run_ids[0])
        assert run_data is not None
        attempts = run_data["attempts"]
        assert len(attempts) == 2  # 1 dev case x 2 sides
        for attempt in attempts:
            usage = attempt["usage"]
            # Real subprocess metering: tokens + synthetic USD pricing.
            assert usage["tokensIn"] > 0
            assert usage["tokensOut"] > 0
            assert attempt["costUsd"] == usage["costUsd"]
            assert attempt["costUsd"] > 0
            assert attempt["adapter"] == "fixture-choose-v1+scenario-client@1"
            assert attempt["status"] == "ok"
        # Evidence sealed from the subprocess workspace into the project store.
        sealed = [
            event
            for event in workspace.journal.events()
            if event.kind is EventKind.AUDIT_NOTE
            and event.data.get("event") == "adapter-evidence-sealed"
        ]
        assert len(sealed) == 1
        digests = sealed[0].data["digests"]
        assert digests
        for digest in digests:
            name = digest.removeprefix("sha256:")
            assert (workspace.vouch_dir / "artifacts" / name).is_file()
        # The adapter's run workspace is cleaned up after collect.
        runs_dir = workspace.vouch_dir / "adapter-workspace" / "runs"
        assert not runs_dir.exists() or not any(runs_dir.iterdir())
    finally:
        workspace.close()


def test_fixture_subprocess_final_acceptance_exports_verified_evidence(
    tmp_path: Path,
) -> None:
    project = init_project(tmp_path, cap=2.0)
    assert baseline(project).exit_code == 0
    assert selection_pack(project).exit_code == 0
    assert (
        invoke(
            "propose",
            "--project",
            str(project),
            "--delta-file",
            "-",
            "--type",
            "prompt-delta",
            "--rationale",
            "fixture acceptance",
            "--id",
            "cand-fx",
            "--seal",
            input_text=DELTA_TEXT,
        ).exit_code
        == 0
    )
    assert evaluate_selection(project, candidate="cand-fx", adapter="fixture").exit_code == 0
    evidence = tmp_path / "evidence"
    result = invoke(
        "accept",
        "--project",
        str(project),
        "--candidate",
        "cand-fx",
        "--pack",
        "w-c3-synthetic",
        "--out",
        str(evidence),
        "--owner",
        "ana",
        "--adapter",
        "fixture",
    )
    assert result.exit_code == 0, out(result)
    assert "verdict=accepted" in result.stdout

    package = verify_package(evidence)
    assert package.package_kind == "evidence"
    attempts = json.loads((evidence / "attempts.json").read_text())["attempts"]
    assert len(attempts) == 2  # final-acceptance case x 2 sides
    for attempt in attempts:
        assert attempt["costUsd"] > 0
        assert attempt["usage"]["tokensIn"] > 0
    # The decision is anchored to exactly these bytes.
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["mode"] == "fixture"
    assert manifest["syntheticOnly"] is True


def test_fixture_adapter_unknown_case_fails_closed(tmp_path: Path) -> None:
    """A case with no scenario mapping is refused, not silently replayed."""
    from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
    from vouch_agent.contracts.common import RunMode, canonical_json, digest_of

    project = init_project(tmp_path)
    assert baseline(project).exit_code == 0
    assert (
        invoke(
            "propose",
            "--project",
            str(project),
            "--delta-file",
            "-",
            "--type",
            "prompt-delta",
            "--rationale",
            "unmapped case",
            "--id",
            "cand-x",
            "--seal",
            input_text=DELTA_TEXT,
        ).exit_code
        == 0
    )

    def case(cid: str, split: CaseSplit) -> TaskCase:
        payload = {"scenario": "compare-normal-zh-cn", "case": {"x": 1}}
        return TaskCase(
            case_id=cid,
            workflow_id="W-C3",
            split=split,
            group_id="synthetic-W-C3",
            input_digest=digest_of(payload),
            synthetic=True,
        )

    hand_pack = TaskPack(
        pack_id="hand-broken",
        workflow_id="W-C3",
        cases=(
            case("case-ghost", CaseSplit.DEVELOPMENT),
            case("compare-normal-zh-cn", CaseSplit.FINAL_ACCEPTANCE),
        ),
        mode=RunMode.FIXTURE,
        notes="hand pack with an unmapped case",
    )
    pack_file = tmp_path / "broken-pack.json"
    blob = canonical_json({"scenario": "compare-normal-zh-cn", "case": {"x": 1}}).encode()
    workspace = ProjectWorkspace.open(project)
    try:
        workspace.artifacts.put(blob)
        pack_file.write_text(json.dumps(hand_pack.to_dict()))
    finally:
        workspace.close()

    result = invoke(
        "evaluate",
        "--project",
        str(project),
        "--candidate",
        "cand-x",
        "--pack",
        str(pack_file),
        "--adapter",
        "fixture",
    )
    combined = out(result)
    assert result.exit_code != 0, combined
    assert "no scenario in fixture pack" in combined
