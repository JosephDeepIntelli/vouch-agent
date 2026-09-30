"""TUI interaction smoke tests (textual pilot).

The TUI is a client of the application services: these tests drive real
screens (open, review rows, export button) with the pilot, assert the
underlying project reflects exactly what the services did, and verify that
exiting the app mid-view leaves project state untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from textual.widgets import DataTable, Input, Static

from vouch_agent.appservices.flow import ImprovementFlow
from vouch_agent.appservices.packs import import_fixture_pack
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.export import verify_package
from vouch_agent.tui.app import VouchApp

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "choose"


def prepare_project(tmp_path: Path) -> tuple[Path, str, str]:
    """Full improvement vertical through the services (no UI), for viewing."""
    directory = tmp_path / "proj"
    spec = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-tui",
            "name": "tui-smoke",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "W-C3",
                    "name": "Compare",
                    "mainObjective": "recordedClaims",
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": 5.0},
        }
    )
    workspace = ProjectWorkspace.create(directory, spec)
    flow = ImprovementFlow(workspace)
    flow.record_baseline(version_id="v0", source_ref="git:tui0")
    # Selection-validation (not development) is the promotion split (A5), and
    # this workflow declares no hard guardrails (the built-in scripted
    # adapter measures none, which would honestly be inconclusive).
    import_fixture_pack(workspace, FIXTURES, "W-C3", dev=0, selection=1, final=1)
    flow.propose(
        delta="+ cite every claim",
        change_type="prompt-delta",
        rationale="tui smoke",
        candidate_id="cand-tui",
        seal=True,
    )
    selection = flow.evaluate(
        candidate_id="cand-tui",
        pack_ref="w-c3-synthetic",
        split=CaseSplit.SELECTION_VALIDATION,
    )
    assert selection.advanced
    acceptance = flow.final_acceptance(candidate_id="cand-tui", pack_ref="w-c3-synthetic")
    decision = flow.record_decision(
        acceptance, owner="ana", evidence_destination=tmp_path / "seed-evidence"
    )
    run_id = acceptance.run.run_id
    decision_id = decision.decision.decision_id
    workspace.close()
    return directory, run_id, decision_id


def snapshot_state(project: Path) -> dict[str, Any]:
    workspace = ProjectWorkspace.open(project)
    try:
        return {
            "candidates": sorted(workspace.store.list_ids("candidate")),
            "candidate_records": [
                workspace.store.load("candidate", cid)
                for cid in workspace.store.list_ids("candidate")
            ],
            "events": len(workspace.journal.events()),
            "costs": len(workspace.journal.cost_entries()),
            "reservations": [r.status.value for r in workspace.ledger.reservations()],
            "runs": sorted(workspace.store.list_ids("evaluation")),
        }
    finally:
        workspace.close()


def test_app_opens_and_review_shows_rows(tmp_path: Path) -> None:
    project, _run_id, _decision_id = prepare_project(tmp_path)

    async def scenario() -> None:
        app = VouchApp(project)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.workspace is not None
            assert app.workspace.spec.project_id == "proj-tui"
            app.switch_screen("review")
            await pilot.pause()
            candidates = app.screen.query_one("#candidates", DataTable)
            decisions = app.screen.query_one("#decisions", DataTable)
            assert candidates.row_count == 1
            assert decisions.row_count == 1
            costs = app.screen.query_one("#costs", Static)
            assert "measured $" in str(costs.content)
            status = app.screen.query_one("#status", Static)
            assert "cand-tui (accepted)" in str(status.content)
            # Run screen is driven by journal events.
            app.switch_screen("run")
            await pilot.pause()
            events = app.screen.query_one("#events", DataTable)
            assert events.row_count > 0
            await pilot.pause()

    asyncio.run(scenario())


def test_export_screen_triggers_export_and_shows_digest(tmp_path: Path) -> None:
    project, run_id, _decision_id = prepare_project(tmp_path)
    destination = tmp_path / "tui-export"

    async def scenario() -> None:
        app = VouchApp(project)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.switch_screen("export")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#run-id", Input).value = run_id
            screen.query_one("#destination", Input).value = str(destination)
            screen.query_one("#btn-evidence").press()
            await pilot.pause()
            await pilot.pause()
            result = str(screen.query_one("#result", Static).content)
            assert "package: evidence" in result
            assert "manifest digest: sha256:" in result
            assert destination.joinpath("manifest.json").is_file()

    asyncio.run(scenario())
    package = verify_package(destination)
    assert package.run_id == run_id
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["mode"] == "fixture"


def test_exit_mid_view_leaves_project_state_untouched(tmp_path: Path) -> None:
    project, _run_id, _decision_id = prepare_project(tmp_path)
    before = snapshot_state(project)

    async def scenario() -> None:
        app = VouchApp(project)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.switch_screen("review")
            await pilot.pause()
            # Look around, refresh the read-only view, then "kill" the client:
            # exit without performing any mutating action.
            app.screen.action_refresh()
            await pilot.pause()
            app.exit()
            await pilot.pause()

    asyncio.run(scenario())
    after = snapshot_state(project)
    assert after == before


def test_open_failure_is_reported_not_crashed(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = VouchApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            screen = app.screen  # project screen is the initial screen
            screen.query_one("#open-path", Input).value = str(tmp_path / "nope")
            opened = app.open_project(str(tmp_path / "nope"), switch=False)
            assert opened is False
            assert app.workspace is None
            status = str(screen.query_one("#status", Static).content)
            assert "open failed" in status
            assert "not a vouch project" in status
            await pilot.pause()

    asyncio.run(scenario())
