"""A7 regressions: repeat pairing by (case, repeat) with retained failures,
and candidate/baseline lineage bound end to end.

Inverted from the coordinator proofs ``failed-repeats-repaired`` (two repeats
shaped fail/ok then ok/fail reassembled into one invented complete pair and
were ACCEPTED) and ``cli-stale-parent`` (candidate based on v0 evaluated,
accepted and approved against baseline v1 while rollback still restored v0).
Controller scenarios drive the public entry points with the port fakes; the
CLI scenario drives the public CLI.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import FIXTURES, baseline, init_project, invoke, out, propose_sealed
from support import (
    ChannelAdapter,
    make_baseline,
    make_candidate,
    make_controller,
    make_pack,
    rubric,
    seed_inputs,
)

from vouch_agent.contracts.candidate import AgentVersion
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role, digest_of
from vouch_agent.contracts.decision import ReleaseRecord
from vouch_agent.contracts.evaluation import EvaluationRun
from vouch_agent.errors import ApprovalInvalidatedError, ContractError
from vouch_agent.evaluation import compare_run, decide, verdict_from_summary

FORGED_BASELINE = "sha256:" + "9" * 64


class AlternatingAdapter(ChannelAdapter):
    """repeat 0: baseline fails; repeat 1: candidate fails. No successful
    same-repeat pair exists — the exact shape of the counterexample."""

    def execute(self, **kwargs):
        result = super().execute(**kwargs)
        case = kwargs["case_input"]
        fail = (case["side"] == "baseline" and case["repeat"] == 0) or (
            case["side"] == "candidate" and case["repeat"] == 1
        )
        return replace(result, ok=not fail, error="measured repeat failure" if fail else None)


def _prepared(cap: float = 1.0):
    controller, store, ledger, journal, artifacts = make_controller(cap)
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "wf-compare")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    return controller, store, ledger, journal, artifacts, pack


def _selection(controller, pack, rubric_digest, *, adapter=None, repeats=1):
    return controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter or ChannelAdapter(channels=("unsafe",)),
        repeats=repeats,
    )


# -- A7(1): failed repeats are never repaired into a pair (controller) -------------


def test_failed_repeats_are_not_repaired_into_one_pair() -> None:
    controller, _store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")  # frozen repeats=1
    run = _selection(
        controller, pack, rubric_digest, adapter=AlternatingAdapter(channels=("unsafe",)),
        repeats=2,  # requested repeats above the frozen minimum
    )
    assert [(a.side.value, a.status.value) for a in run.attempts] == [
        ("baseline", "failed"),
        ("candidate", "ok"),
        ("baseline", "ok"),
        ("candidate", "failed"),
    ]
    # repeat identity is persisted on every attempt (v1.2 contract, A7)
    assert [a.repeat_index for a in run.attempts] == [0, 0, 1, 1]
    assert all(a.retry_of is None for a in run.attempts)  # no undeclared retries

    frozen = controller.rubric(rubric_digest)
    summary = compare_run(run, controller.project.workflow("wf-compare"), frozen)
    # THE inversion: 0 complete same-repeat pairs, not one invented pair
    assert summary.complete_repeat_pairs == 0
    assert summary.complete_pairs == 0
    assert summary.pairs_below_requested_repeats == ("case-sel-1",)
    verdict = verdict_from_summary(summary, frozen)
    assert verdict.value != "accepted"
    # the measured candidate failure on repeat 1 is a measured bad outcome
    assert verdict.value == "rejected"

    # and it cannot promote the candidate either
    with pytest.raises(ContractError, match="selection"):
        controller.mark_evaluated("cand-acc")
    assert controller.candidate("cand-acc").state.value == "sealed"


def test_controller_sends_the_known_version_digest_for_each_side() -> None:
    """A6/A7: the controller knows each side's version digest and always puts
    it in the adapter execution input (protocol v1.1 §1)."""
    controller, _store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter(channels=("unsafe",))
    run = _selection(controller, pack, rubric_digest, adapter=adapter)
    sent = {(i["side"], i.get("versionDigest")) for i in adapter.executed_case_inputs}
    assert ("baseline", run.baseline_digest) in sent
    assert ("candidate", controller.candidate("cand-acc").content_digest()) in sent
    assert all(i.get("versionDigest") for i in adapter.executed_case_inputs)


def test_sealed_attempt_output_carries_the_observed_mode_and_runner_revision() -> None:
    """A5/A7: mode + runnerVersion of the recorded exchange are sealed with
    the attempt output, so attestation can derive them from evidence."""
    controller, _store, _ledger, _journal, artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter(channels=("unsafe",))
    run = _selection(controller, pack, rubric_digest, adapter=adapter)
    for attempt in run.attempts:
        assert attempt.output_digest
        sealed = json.loads(artifacts.get(attempt.output_digest).decode("utf-8"))
        assert sealed["mode"] == "fixture"
        assert sealed["runnerVersion"] == adapter.adapter_id
        assert sealed["repeatIndex"] == attempt.repeat_index


def test_genuine_two_repeat_run_still_accepts() -> None:
    """The pairing change must not break the honest path."""
    controller, _store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(repeats=2), "ana")
    adapter = ChannelAdapter(
        channels=("unsafe",), baseline_values=(1.0, 1.0), candidate_values=(2.0, 2.0)
    )
    run = _selection(controller, pack, rubric_digest, adapter=adapter, repeats=2)
    frozen = controller.rubric(rubric_digest)
    summary = compare_run(run, controller.project.workflow("wf-compare"), frozen)
    assert summary.complete_repeat_pairs == 2
    assert verdict_from_summary(summary, frozen).value == "accepted"


# -- A7(2): candidate/baseline lineage ----------------------------------------------


def test_evaluation_refuses_when_candidate_parent_is_not_the_measured_baseline() -> None:
    controller, _store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    new_baseline = AgentVersion(version_id="v1", source_ref="git:new")
    with pytest.raises(ContractError, match="rebase the candidate explicitly"):
        controller.run_paired_evaluation(
            workflow_id="wf-compare",
            candidate_id="cand-acc",
            baseline=new_baseline,
            pack=pack,
            split=CaseSplit.SELECTION_VALIDATION,
            rubric_digest=rubric_digest,
            adapter=ChannelAdapter(channels=("unsafe",)),
        )
    assert controller.candidate("cand-acc").state.value == "sealed"

    # final acceptance refuses the same way once the candidate qualifies
    _selection(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")
    with pytest.raises(ContractError, match="rebase the candidate explicitly"):
        controller.run_final_acceptance(
            workflow_id="wf-compare",
            candidate_id="cand-acc",
            baseline=new_baseline,
            pack=pack,
            rubric_digest=rubric_digest,
            adapter=ChannelAdapter(channels=("unsafe",)),
            role=Role.ACCEPTANCE_OWNER,
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"


def test_explicit_rebase_produces_a_new_digest_and_fresh_evidence_requirement() -> None:
    controller, _store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    new_baseline = AgentVersion(version_id="v1", source_ref="git:new")
    old_digest = controller.candidate("cand-acc").content_digest()

    rebased = controller.rebase_candidate(
        "cand-acc", baseline=new_baseline, workflow_id="wf-compare", note="baseline moved"
    )
    assert rebased.candidate_id != "cand-acc"
    assert rebased.state.value == "proposed"
    assert rebased.parent_version.digest() == new_baseline.digest()
    assert rebased.content_digest() != old_digest  # fresh evidence is required
    assert rebased.delta == controller.candidate("cand-acc").delta

    # the rebased candidate evaluates cleanly against the new baseline
    controller.seal(rebased.candidate_id)
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id=rebased.candidate_id,
        baseline=new_baseline,
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=ChannelAdapter(channels=("unsafe",)),
    )
    assert run.baseline_digest == new_baseline.digest()

    # rebasing an already-current parent is refused (nothing to do)
    with pytest.raises(ContractError, match="already based on baseline"):
        controller.rebase_candidate(
            rebased.candidate_id, baseline=new_baseline, workflow_id="wf-compare"
        )


def test_decision_and_approval_refuse_a_stale_parent_lineage() -> None:
    """A durable run claiming a baseline the candidate was not derived from
    is refused at decision AND at approval/release — the review's gap was
    that approval compared only the active baseline with the run baseline."""
    controller, store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")
    frozen = controller.rubric(rubric_digest)
    workflow = controller.project.workflow("wf-compare")

    final = controller.run_final_acceptance(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        rubric_digest=rubric_digest,
        adapter=ChannelAdapter(channels=("unsafe",)),
        role=Role.ACCEPTANCE_OWNER,
    )

    def _stored() -> EvaluationRun:
        data = store.load("evaluation", final.run_id)
        assert data is not None
        return EvaluationRun.from_dict(data)

    def _rewrite(baseline_digest: str) -> None:
        data = store.load("evaluation", final.run_id)
        assert data is not None
        data["baselineDigest"] = baseline_digest
        store.save("evaluation", final.run_id, data)

    evidence_digest = digest_of({"export": final.run_id})
    store.save("run-export", final.run_id, {"manifestDigest": evidence_digest, "path": "/x"})

    # (a) the run measured a baseline the candidate was not derived from
    _rewrite(FORGED_BASELINE)
    tampered = _stored()
    summary = compare_run(tampered, workflow, frozen)
    decision = decide(summary, tampered, frozen, owner="ana", evidence_digest=evidence_digest)
    with pytest.raises(ContractError, match="rebase the candidate explicitly"):
        controller.record_decision(
            decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final.run_id
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"

    # (b) drift AFTER a recorded acceptance: approval and release both refuse
    _rewrite(make_baseline().digest())
    honest = _stored()
    honest_summary = compare_run(honest, workflow, frozen)
    accepted = decide(
        honest_summary, honest, frozen, owner="ana", evidence_digest=evidence_digest
    )
    controller.record_decision(
        accepted, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final.run_id
    )
    assert controller.candidate("cand-acc").state.value == "accepted"

    _rewrite(FORGED_BASELINE)
    with pytest.raises(ApprovalInvalidatedError):
        controller.record_approval(
            "cand-acc", role=Role.RELEASE_OWNER, binding=accepted.binding
        )
    with pytest.raises(ApprovalInvalidatedError):
        controller.record_release(
            ReleaseRecord(
                release_id="rel-x",
                candidate_digest=accepted.candidate_digest,
                deployed_version="v0",
                deployed_by="roger",
            ),
            role=Role.RELEASE_OWNER,
        )
    assert controller.candidate("cand-acc").state.value == "accepted"


# -- A7(2) at the public CLI ----------------------------------------------------------


def _selection_pack(project: Path) -> None:
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


def _candidate_state(project: Path) -> str:
    from vouch_agent.appservices.workspace import ProjectWorkspace

    workspace = ProjectWorkspace.open(project)
    try:
        data = workspace.store.load("candidate", "cand-1")
        assert data is not None
        return str(data["state"])
    finally:
        workspace.close()


def test_cli_stale_parent_chain_breaks_at_evaluation(tmp_path: Path) -> None:
    """The CLI counterexample, inverted: after the baseline moves to v1, the
    v0-parented candidate can no longer be evaluated, accepted or approved —
    the chain refuses at the FIRST stateful step instead of silently
    measuring the candidate against a baseline it was not derived from."""
    project = init_project(tmp_path, cap=5.0)
    assert baseline(project).exit_code == 0  # v0 / git:abc0
    _selection_pack(project)
    assert propose_sealed(project).exit_code == 0
    assert baseline(project, version="v1", ref="git:new").exit_code == 0

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
    assert "rebase" in combined
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
        str(tmp_path / "evidence"),
        "--owner",
        "ana",
    )
    assert accept.exit_code != 0, out(accept)
    assert _candidate_state(project) == "sealed"


def test_cli_honest_chain_still_completes(tmp_path: Path) -> None:
    """No baseline drift: the whole improvement tail still works."""
    project = init_project(tmp_path, cap=5.0)
    assert baseline(project).exit_code == 0
    _selection_pack(project)
    assert propose_sealed(project).exit_code == 0
    selection = invoke(
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
    assert selection.exit_code == 0, out(selection)
    assert "candidate advanced to 'evaluated'" in selection.stdout
    accepted = invoke(
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
    assert accepted.exit_code == 0, out(accepted)
    assert invoke("approve", "--project", str(project), "--candidate", "cand-1").exit_code == 0
    rollback = invoke(
        "export",
        "--project",
        str(project),
        "--rollback",
        "--candidate",
        "cand-1",
        "--out",
        str(tmp_path / "rollback"),
    )
    assert rollback.exit_code == 0, out(rollback)
    data = json.loads((tmp_path / "rollback" / "rollback.json").read_text())
    # lineage and rollback agree: the revert targets the measured parent
    assert data["revert"]["sourceRef"] == "git:abc0"
