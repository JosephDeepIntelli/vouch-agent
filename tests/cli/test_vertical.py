"""CLI improvement vertical: happy paths and fail-closed refusals.

Every fail-closed case asserts a non-zero exit AND the error taxonomy code
being visible (e.g. ``vouch/budget-exhausted``), so the CLI contract "errors
are distinguishable without string-matching the prose" holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import (
    DELTA_TEXT,
    FIXTURES,
    baseline,
    evaluate_selection,
    fixture_pack,
    init_project,
    invoke,
    out,
    propose_sealed,
    selection_pack,
)

from vouch_agent.appservices.flow import ImprovementFlow
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role
from vouch_agent.contracts.evaluation import Rubric
from vouch_agent.errors import ContractError, SplitAccessError
from vouch_agent.export import verify_package


class TestInfoCommands:
    def test_version(self) -> None:
        result = invoke("version")
        assert result.exit_code == 0
        assert "vouch-agent" in result.stdout
        assert "jaz-lang" in result.stdout

    def test_modes(self) -> None:
        result = invoke("modes")
        assert result.exit_code == 0
        assert "fixture: live-calls=False" in result.stdout
        assert "authorized-live: live-calls=True" in result.stdout


class TestInit:
    def test_init_prints_what_was_frozen(self, tmp_path: Path) -> None:
        project = tmp_path / "p1"
        result = invoke(
            "init",
            "--project",
            str(project),
            "--workflow",
            "W-C3",
            "--workflow",
            "custom-wf=My Custom Flow",
            "--cap",
            "7.5",
            "--owners",
            "acceptance-owner=ana",
            "--owners",
            "release-owner=roger",
            "--guardrail",
            "fabricated-citation",
        )
        assert result.exit_code == 0, out(result)
        assert (project / ".vouch" / "workspace.json").is_file()
        assert "frozen workflow W-C3: Compare" in result.stdout
        assert "frozen workflow custom-wf: My Custom Flow" in result.stdout
        assert "fabricated-citation" in result.stdout
        assert "acceptance-owner" in result.stdout

    def test_init_requires_owner_roles(self, tmp_path: Path) -> None:
        result = invoke("init", "--project", str(tmp_path / "p"), "--workflow", "W-C3")
        assert result.exit_code != 0
        assert "acceptance-owner" in out(result)

    def test_init_refuses_unknown_workflow(self, tmp_path: Path) -> None:
        result = invoke(
            "init",
            "--project",
            str(tmp_path / "p"),
            "--workflow",
            "W-C99",
            "--owners",
            "acceptance-owner=a",
            "--owners",
            "release-owner=r",
        )
        assert result.exit_code != 0
        assert "unknown workflow" in out(result)

    def test_init_refuses_reinit(self, tmp_path: Path) -> None:
        project = init_project(tmp_path)
        result = invoke(
            "init",
            "--project",
            str(project),
            "--workflow",
            "W-C3",
            "--owners",
            "acceptance-owner=ana",
            "--owners",
            "release-owner=roger",
        )
        assert result.exit_code != 0
        assert "already exists" in out(result)


class TestBaseline:
    def test_baseline_freezes_rubric(self, tmp_path: Path) -> None:
        project = init_project(tmp_path)
        result = baseline(project, version="v1", ref="git:deadbeef")
        assert result.exit_code == 0, out(result)
        assert "W-C3:v1" in result.stdout
        assert "rubric frozen" in result.stdout
        assert "frozen by: ana" in result.stdout

    def test_baseline_refuses_unknown_workflow(self, tmp_path: Path) -> None:
        project = init_project(tmp_path)
        result = invoke(
            "baseline",
            "--project",
            str(project),
            "--version",
            "v0",
            "--source-ref",
            "git:x",
            "--workflow",
            "W-C8",
        )
        assert result.exit_code != 0
        assert "[vouch/contract]" in out(result)
        assert "unknown workflow" in out(result)

    def test_evaluate_without_baseline_fails_closed(self, tmp_path: Path) -> None:
        project = init_project(tmp_path)
        assert fixture_pack(project).exit_code == 0
        # Proposing needs the baseline parent; evaluating needs it too.
        result = propose_sealed(project)
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "no recorded baseline" in combined
        result = invoke(
            "evaluate",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        # The candidate was never created (propose needs the baseline too).
        assert "[vouch/contract]" in combined
        assert "unknown candidate 'cand-1'" in combined


class TestPropose:
    def test_propose_from_stdin_shows_seal_preview(self, project: Path) -> None:
        result = invoke(
            "propose",
            "--project",
            str(project),
            "--delta-file",
            "-",
            "--type",
            "prompt-delta",
            "--rationale",
            "second candidate",
            "--id",
            "cand-2",
            input_text=DELTA_TEXT,
        )
        assert result.exit_code == 0, out(result)
        assert "cand-2" in result.stdout
        assert "seal preview (content digest): sha256:" in result.stdout
        assert "not sealed" in result.stdout

    def test_propose_empty_delta_refused(self, project: Path) -> None:
        result = invoke(
            "propose",
            "--project",
            str(project),
            "--delta-file",
            "-",
            "--type",
            "prompt-delta",
            "--rationale",
            "x",
        )
        # CliRunner feeds empty stdin; the CLI refuses empty deltas.
        assert result.exit_code != 0
        assert "delta is empty" in out(result)

    def test_propose_out_of_scope_change_type_fails_closed(self, tmp_path: Path) -> None:
        project = init_project(tmp_path, change_types=["prompt-delta"])
        assert baseline(project).exit_code == 0
        result = invoke(
            "propose",
            "--project",
            str(project),
            "--delta-file",
            "-",
            "--type",
            "retrieval-params",
            "--rationale",
            "not allowed here",
            input_text=DELTA_TEXT,
        )
        assert result.exit_code != 0
        combined = out(result)
        assert "[vouch/contract]" in combined
        assert "not in allowed_change_types" in combined

    def test_propose_missing_delta_file(self, project: Path) -> None:
        result = invoke(
            "propose",
            "--project",
            str(project),
            "--delta-file",
            str(project / "nope.md"),
            "--type",
            "prompt-delta",
            "--rationale",
            "x",
            input_text=DELTA_TEXT,
        )
        assert result.exit_code != 0
        assert "does not exist" in out(result)


class TestEvaluateScripted:
    def test_development_evaluation_does_not_promote(self, project: Path) -> None:
        """A development-only acceptance is honest progress, not selection
        qualification (review A5): the candidate stays sealed."""
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
        assert "pairs: 1/1 complete" in result.stdout
        assert "verdict: accepted" in result.stdout
        assert "candidate stays 'sealed'" in result.stdout
        assert "mode: fixture" in result.stdout

    def test_selection_validation_evaluation_advances(self, project: Path) -> None:
        assert selection_pack(project).exit_code == 0
        result = evaluate_selection(project)
        assert result.exit_code == 0, out(result)
        assert "pairs: 1/1 complete" in result.stdout
        assert "verdict: accepted" in result.stdout
        assert "candidate advanced to 'evaluated'" in result.stdout

    def test_unsealed_candidate_fails_closed(self, tmp_path: Path) -> None:
        directory = init_project(tmp_path)
        assert baseline(directory).exit_code == 0
        assert fixture_pack(directory).exit_code == 0
        result = invoke(
            "propose",
            "--project",
            str(directory),
            "--delta-file",
            "-",
            "--type",
            "prompt-delta",
            "--rationale",
            "not sealed",
            "--id",
            "cand-open",
            input_text=DELTA_TEXT,
        )
        assert result.exit_code == 0, out(result)
        result = invoke(
            "evaluate",
            "--project",
            str(directory),
            "--candidate",
            "cand-open",
            "--pack",
            "w-c3-synthetic",
        )
        assert result.exit_code != 0
        combined = out(result)
        assert "[vouch/contract]" in combined
        assert "must be sealed" in combined

    def test_budget_exhaustion_fails_closed_with_code(self, tmp_path: Path) -> None:
        directory = init_project(tmp_path, cap=0.05)
        assert baseline(directory).exit_code == 0
        assert fixture_pack(directory, dev=1, final=0).exit_code == 0
        assert propose_sealed(directory).exit_code == 0
        result = invoke(
            "evaluate",
            "--project",
            str(directory),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "[vouch/budget-exhausted]" in combined

    def test_tampered_pack_fails_closed(self, tmp_path: Path) -> None:
        directory = init_project(tmp_path)
        assert baseline(directory).exit_code == 0
        pack_file = tmp_path / "pack.json"
        result = invoke(
            "pack",
            "--project",
            str(directory),
            "--from-fixture",
            str(FIXTURES),
            "--workflow",
            "W-C3",
            "--dev",
            "1",
            "--final",
            "1",
            "--out",
            str(pack_file),
        )
        assert result.exit_code == 0, out(result)
        data = json.loads(pack_file.read_text())
        # Tamper: point a case at a digest nobody can produce bytes for.
        data["cases"][0]["inputDigest"] = "sha256:" + "0" * 64
        pack_file.write_text(json.dumps(data))
        assert propose_sealed(directory).exit_code == 0
        result = invoke(
            "evaluate",
            "--project",
            str(directory),
            "--candidate",
            "cand-1",
            "--pack",
            str(pack_file),
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "[vouch/contract]" in combined
        assert "does not hold" in combined

    def test_unknown_pack_fails_closed(self, project: Path) -> None:
        result = invoke(
            "evaluate",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "ghost-pack",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "ghost-pack" in combined

    def test_unknown_role_fails_closed(self, project: Path) -> None:
        result = invoke(
            "evaluate",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
            "--role",
            "wizard",
        )
        assert result.exit_code != 0
        assert "unknown role" in out(result)

    def test_evaluation_refuses_final_acceptance_split(self, project: Path) -> None:
        result = invoke(
            "evaluate",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
            "--split",
            "final-acceptance",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "final-acceptance flow" in combined


class TestWrongRoleServiceLevel:
    """Role refusals at the service layer (what the CLI/TUI both call)."""

    def _prepared(self, project: Path) -> ImprovementFlow:
        workspace = ProjectWorkspace.open(project)
        return ImprovementFlow(workspace)

    def test_proposer_may_not_run_final_acceptance(self, project: Path) -> None:
        flow = self._prepared(project)
        with pytest.raises(SplitAccessError) as excinfo:
            flow.final_acceptance(
                candidate_id="cand-1",
                pack_ref="w-c3-synthetic",
                role=Role.PROPOSER,
            )
        assert "acceptance" in str(excinfo.value)
        flow.workspace.close()

    def test_engineer_may_not_run_final_acceptance(self, project: Path) -> None:
        flow = self._prepared(project)
        with pytest.raises(SplitAccessError):
            flow.final_acceptance(
                candidate_id="cand-1",
                pack_ref="w-c3-synthetic",
                role=Role.ENGINEER,
            )
        flow.workspace.close()

    def test_engineer_may_not_see_final_acceptance_cases(self, project: Path) -> None:
        flow = self._prepared(project)
        pack = flow.load_pack("w-c3-synthetic", role=Role.PROPOSER)
        assert not pack.cases_in(CaseSplit.FINAL_ACCEPTANCE)
        assert pack.cases_in(CaseSplit.DEVELOPMENT)
        flow.workspace.close()

    def test_release_requires_release_owner_role(self, project: Path) -> None:
        flow = self._prepared(project)
        with pytest.raises(ContractError, match="release-owner required"):
            flow.record_release(
                "cand-1",
                deployed_version="x@1",
                deployed_by="someone",
                role=Role.ENGINEER,
            )
        flow.workspace.close()


class TestUnfrozenRubric:
    def test_unfrozen_rubric_blocks_evaluation(self, project: Path) -> None:
        workspace = ProjectWorkspace.open(project)
        # Tamper the workflow->rubric mapping onto an UNFROZEN rubric record,
        # as if someone hand-edited the store: the controller must refuse.
        unfrozen = Rubric(main_metric="recordedClaims", direction="increase")
        workspace.store.save("rubric", unfrozen.digest(), unfrozen.to_dict())
        workspace.store.save(
            "workflow-rubric",
            "W-C3",
            {"rubricDigest": unfrozen.digest()},
        )
        workspace.close()
        result = invoke(
            "evaluate",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "frozen" in combined


class TestAcceptApproveRelease:
    def test_full_tail_of_the_vertical(self, project: Path, tmp_path: Path) -> None:
        assert selection_pack(project).exit_code == 0
        assert evaluate_selection(project).exit_code == 0
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
        assert "verdict=accepted" in result.stdout
        assert "manifest digest: sha256:" in result.stdout
        assert "candidate state: accepted" in result.stdout
        package = verify_package(evidence)
        assert package.package_kind == "evidence"

        result = invoke("approve", "--project", str(project), "--candidate", "cand-1")
        assert result.exit_code == 0, out(result)
        assert "approved cand-1" in result.stdout

        result = invoke(
            "release",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--deployed-version",
            "choose@v9",
            "--deployed-by",
            "roger",
        )
        assert result.exit_code == 0, out(result)
        assert "released cand-1" in result.stdout

    def test_accept_refuses_proposer_as_owner(self, project: Path, tmp_path: Path) -> None:
        assert selection_pack(project).exit_code == 0
        assert evaluate_selection(project).exit_code == 0
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
            "proposer-agent",  # the candidate proposer's actual identity
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "cannot be the acceptance owner" in combined
        # a literal "proposer" is equally refused: it is not the configured
        # acceptance owner either
        result = invoke(
            "accept",
            "--project",
            str(project),
            "--candidate",
            "cand-1",
            "--pack",
            "w-c3-synthetic",
            "--out",
            str(tmp_path / "ev2"),
            "--owner",
            "proposer",
        )
        combined = out(result)
        assert result.exit_code != 0, combined
        assert "acceptance owner" in combined

    def test_accept_before_evaluation_fails_closed(self, project: Path, tmp_path: Path) -> None:
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
        assert "completed selection evaluation" in combined
