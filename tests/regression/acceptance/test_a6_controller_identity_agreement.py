"""A6 regressions (controller side): an adapter that misreports the provider
mode never has its result stored, accepted or attested.

The transport-level agreement (frame run id, identity echo, payload mode) is
covered by ``tests/adapters/test_a6_protocol_identity_agreement.py``; here the
trusted controller path itself is exercised with an in-process adapter — the
controller must not depend on the process boundary for identity agreement.
"""

from __future__ import annotations

from support import (
    ChannelAdapter,
    make_baseline,
    make_candidate,
    make_controller,
    make_pack,
    rubric,
    seed_inputs,
)

from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role, RunMode
from vouch_agent.errors import ContractError


class LyingModeAdapter(ChannelAdapter):
    """Answers every execute with mode=authorized-live regardless of the
    request — the in-process shape of the counterexample."""

    def execute(self, **kwargs):
        result = super().execute(**kwargs)
        from dataclasses import replace

        return replace(result, mode=RunMode.AUTHORIZED_LIVE)


def _prepared():
    controller, store, ledger, journal, artifacts = make_controller()
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "wf-compare")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    return controller, store, ledger, journal, artifacts, pack


def test_adapter_reporting_a_different_mode_fails_before_storage() -> None:
    controller, store, _ledger, journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = LyingModeAdapter(channels=("unsafe",))
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    # every attempt is a failure carrying the identity contradiction
    assert run.attempts
    for attempt in run.attempts:
        assert attempt.status.value == "failed"
        assert attempt.error is not None and "mode" in attempt.error
        assert attempt.output_digest is None  # nothing of it was sealed
        assert attempt.usage == {}  # its metering is not trusted either
        stored = store.load("attempt", attempt.attempt_id)
        assert stored is not None and stored["status"] == "failed"
    # and the failed run cannot qualify the candidate
    try:
        controller.mark_evaluated("cand-acc")
    except ContractError:
        pass
    else:  # pragma: no cover - the guard above must fire
        raise AssertionError("a run of misreported-mode attempts qualified a candidate")
    assert controller.candidate("cand-acc").state.value == "sealed"
    # the failure is journaled (truthful usage), not swallowed
    assert any(
        e.kind.value == "attempt-ended" and e.data.get("status") == "failed"
        for e in journal.events()
    )
    assert adapter.executed_case_inputs  # the attempts really were dispatched


def test_honest_in_process_adapter_still_records_ok_attempts() -> None:
    controller, _store, _ledger, _journal, artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter(channels=("unsafe",))
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    assert all(a.status.value == "ok" for a in run.attempts)
    for attempt in run.attempts:
        assert attempt.output_digest and artifacts.exists(attempt.output_digest)
