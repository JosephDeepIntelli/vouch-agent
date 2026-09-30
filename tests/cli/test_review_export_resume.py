"""CLI review / export / resume: honest reporting and explicit recovery."""

from __future__ import annotations

from pathlib import Path

from helpers import evaluate_selection, invoke, out, selection_pack

from vouch_agent.appservices.flow import ImprovementFlow
from vouch_agent.appservices.reporting import ReportService
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.journal import ReservationStatus


def _evaluated(project: Path) -> None:
    """Promote cand-1 through a qualifying selection-validation run (A5)."""
    assert selection_pack(project).exit_code == 0
    result = evaluate_selection(project)
    assert result.exit_code == 0, out(result)


class TestReview:
    def test_review_lists_candidates_pending_and_costs(self, project: Path) -> None:
        _evaluated(project)
        result = invoke("review", "--project", str(project))
        assert result.exit_code == 0, out(result)
        assert "cand-1: evaluated" in result.stdout
        assert "independent final acceptance by the acceptance side" in result.stdout
        assert "hard guardrail findings: none" in result.stdout
        assert "costs: measured $" in result.stdout
        assert "workflow coverage:" in result.stdout
        assert "runner-integrated needs a real runner" in result.stdout

    def test_review_after_decision_shows_decision_rows(self, project: Path, tmp_path: Path) -> None:
        _evaluated(project)
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
                str(tmp_path / "ev"),
                "--owner",
                "ana",
            ).exit_code
            == 0
        )
        result = invoke("review", "--project", str(project))
        assert result.exit_code == 0, out(result)
        assert "dec_" in result.stdout
        assert "accepted by ana" in result.stdout
        assert "release-owner approval" in result.stdout


class TestExport:
    def test_evidence_export_prints_manifest_digest(self, project: Path, tmp_path: Path) -> None:
        _evaluated(project)
        destination = tmp_path / "exported"
        result = invoke(
            "export",
            "--project",
            str(project),
            "--run",
            _first_run(project),
            "--out",
            str(destination),
        )
        assert result.exit_code == 0, out(result)
        assert "package: evidence" in result.stdout
        assert "manifest digest: sha256:" in result.stdout
        assert destination.joinpath("manifest.json").is_file()

    def test_evidence_export_refuses_non_empty_destination(
        self, project: Path, tmp_path: Path
    ) -> None:
        _evaluated(project)
        destination = tmp_path / "busy"
        destination.mkdir()
        (destination / "stray.txt").write_text("x")
        result = invoke(
            "export",
            "--project",
            str(project),
            "--run",
            _first_run(project),
            "--out",
            str(destination),
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "not empty" in combined

    def test_export_refuses_unknown_run(self, project: Path, tmp_path: Path) -> None:
        result = invoke(
            "export", "--project", str(project), "--run", "eval_ghost", "--out", str(tmp_path / "x")
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "unknown evaluation run" in combined

    def test_rollback_export_describes_revert_never_deploys(
        self, project: Path, tmp_path: Path
    ) -> None:
        destination = tmp_path / "rollback"
        result = invoke(
            "export",
            "--project",
            str(project),
            "--rollback",
            "--candidate",
            "cand-1",
            "--out",
            str(destination),
        )
        assert result.exit_code == 0, out(result)
        assert "package: rollback-plan" in result.stdout
        rollback = (destination / "rollback.json").read_text()
        assert '"deployAction": "none"' in rollback
        assert '"deployActionPerformed": false' in rollback


def _first_run(project: Path) -> str:
    workspace = ProjectWorkspace.open(project)
    try:
        return workspace.store.list_ids("evaluation")[0]
    finally:
        workspace.close()


class TestResume:
    def test_resume_clean_when_no_recovery_work(self, project: Path) -> None:
        result = invoke("resume", "--project", str(project))
        assert result.exit_code == 0, out(result)
        assert "open budget reservations: none" in result.stdout
        assert "needs-reconciliation subjects: none" in result.stdout
        assert "recovery: clean" in result.stdout

    def test_resume_reports_stale_open_reservation(self, project: Path) -> None:
        workspace = ProjectWorkspace.open(project)
        try:
            stale = workspace.ledger.reserve("crashed-holder", 0.30)
        finally:
            workspace.close()
        result = invoke("resume", "--project", str(project))
        assert result.exit_code == 0, out(result)
        assert stale.reservation_id in result.stdout
        assert "open budget reservation" in result.stdout

    def test_reconcile_refuses_empty_verified_note(self, project: Path) -> None:
        workspace = ProjectWorkspace.open(project)
        try:
            stale = workspace.ledger.reserve("crashed-holder", 0.30)
        finally:
            workspace.close()
        result = invoke(
            "resume",
            "--project",
            str(project),
            "--reconcile",
            stale.reservation_id,
            "--verified-note",
            "   ",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "[vouch/reconciliation-required]" in combined
        assert "verified" in combined

    def test_reconcile_releases_stale_reservation_with_note(self, project: Path) -> None:
        workspace = ProjectWorkspace.open(project)
        try:
            stale = workspace.ledger.reserve("crashed-holder", 0.30)
            workspace.close()
            flow = ImprovementFlow(ProjectWorkspace.open(project))
            flow.reconcile(
                stale.reservation_id,
                "queried the runner: no work started, hold can return",
                release=True,
            )
            flow.workspace.close()
            workspace = ProjectWorkspace.open(project)
            state = ReportService(workspace).resume_state()
            assert state.open_reservations == []
            record = workspace.ledger.reservations()[0]
            assert record.status is ReservationStatus.RELEASED
        finally:
            workspace.close()
        result = invoke("resume", "--project", str(project))
        assert "recovery: clean" in result.stdout

    def test_reconcile_settles_verified_spend(self, project: Path) -> None:
        workspace = ProjectWorkspace.open(project)
        try:
            stale = workspace.ledger.reserve("crashed-holder", 0.30)
            workspace.close()
            flow = ImprovementFlow(ProjectWorkspace.open(project))
            flow.reconcile(
                stale.reservation_id,
                "runner log shows the attempt completed; $0.12 actually spent",
                settle_usd=0.12,
            )
            flow.workspace.close()
            workspace = ProjectWorkspace.open(project)
            record = workspace.ledger.reservations()[0]
            assert record.status is ReservationStatus.SETTLED
            assert record.settled_amount_usd == 0.12
        finally:
            workspace.close()

    def test_resume_reports_needs_reconciliation_subject(self, project: Path) -> None:
        workspace = ProjectWorkspace.open(project)
        try:
            ImprovementFlow(workspace).mark_needs_reconciliation(
                "eval_crashed", "side-effect state unknown after crash"
            )
            workspace.close()
            result = invoke("resume", "--project", str(project))
            assert result.exit_code == 0, out(result)
            assert "eval_crashed" in result.stdout
            flow = ImprovementFlow(ProjectWorkspace.open(project))
            flow.reconcile("eval_crashed", "queried the target system: nothing shipped")
            flow.workspace.close()
            result = invoke("resume", "--project", str(project))
            assert "needs-reconciliation subjects: none" in result.stdout
        finally:
            workspace.close()
