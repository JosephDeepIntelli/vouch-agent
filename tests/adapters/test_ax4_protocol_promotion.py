"""Positive promotion path over the REAL framed protocol (M4 A2).

Drives the real controller → :class:`ProcessAdapterClient` framed protocol →
real child process → durable store path end to end: a genuine describe()
handshake, genuine execute exchanges with identity echo, sealed attempts,
then promotion, reload and re-verification through the manifest's one
promotion authority. No port fakes on the adapter path — the only synthetic
element is the child responder itself (deterministic, fixture mode).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
_ACCEPTANCE = _TESTS / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

from support import (  # noqa: E402
    FakeArtifacts,
    FakeJournal,
    FakeLedger,
    FakeStore,
    make_baseline,
    make_candidate,
    rubric,
    seed_inputs,
)

from vouch_agent.adapters.fixture_adapter import FixturePack  # noqa: E402
from vouch_agent.adapters.process_adapter import ProcessAdapterClient  # noqa: E402
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack  # noqa: E402
from vouch_agent.contracts.common import Role, RunMode, digest_of  # noqa: E402
from vouch_agent.contracts.project import ProjectSpec  # noqa: E402
from vouch_agent.controller import VouchController  # noqa: E402
from vouch_agent.evaluation.attestation_evidence import descriptor_digest  # noqa: E402
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass  # noqa: E402
from vouch_agent.workflows.manifest import (  # noqa: E402
    CoverageLayer,
    RunnerIntegrationRecord,
    WorkflowManifest,
)

REPO_ROOT = _TESTS.parent
FIXTURES = REPO_ROOT / "fixtures" / "choose"

RUNNER_ID = "ax4-protocol-child@1"

_CHILD = (
    "import json, sys\n"
    "seq = 0\n"
    "for line in sys.stdin:\n"
    "    req = json.loads(line)\n"
    "    kind = req['kind']\n"
    "    payload = req.get('payload') or {}\n"
    "    run_id = req.get('runId')\n"
    "    if kind == 'describe-request':\n"
    "        answer = {'adapterId': '" + RUNNER_ID + "', 'protocolVersion': '1',\n"
    "                  'workflows': ['W-C3'], 'enforcedModes': ['fixture']}\n"
    "    elif kind in ('prepare-request', 'cleanup-request'):\n"
    "        answer = {'ok': True}\n"
    "    elif kind == 'execute-request':\n"
    "        case_input = payload['caseInput']\n"
    "        baseline = case_input.get('side') == 'baseline'\n"
    "        answer = {'ok': True, 'mode': 'fixture', 'runnerVersion': '" + RUNNER_ID + "',\n"
    "                  'outputs': {'caseId': case_input.get('caseId')},\n"
    "                  'usage': {'score': 1.0 if baseline else 2.0, 'costUsd': 0.01},\n"
    "                  'identity': payload['identity']}\n"
    "    elif kind == 'collect-request':\n"
    "        answer = {'digests': []}\n"
    "    else:\n"
    "        break\n"
    "    print(json.dumps({'protocolVersion': '1', 'seq': seq,\n"
    "                      'kind': kind.replace('-request', '-response'),\n"
    "                      'runId': run_id, 'payload': answer}), flush=True)\n"
    "    seq += 1\n"
)


def _controller_with_protocol_run():
    """A real controller evaluation whose adapter is the REAL framed protocol."""
    store, artifacts, ledger, journal = FakeStore(), FakeArtifacts(), FakeLedger(1.0), FakeJournal()
    project = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-ax4-protocol",
            "name": "ax4 protocol promotion regression",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "W-C3",
                    "name": "Compare",
                    "mainObjective": "score",
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": 1.0},
        }
    )
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            allowed_resources=frozenset(),
            resource_prefixes=("case-", "eval_"),
            max_reservation_usd=0.5,
        ),
        ledger,
        journal,
    )
    controller = VouchController(project, store, artifacts, ledger, journal, broker)
    controller.init()
    selection_case, final_case = "case-w-c3-sel-1", "case-w-c3-fin-1"
    pack = TaskPack(
        pack_id="pack-ax4-protocol",
        workflow_id="W-C3",
        cases=(
            TaskCase(
                case_id=selection_case,
                workflow_id="W-C3",
                split=CaseSplit.SELECTION_VALIDATION,
                group_id="family-ax4-protocol",
                input_digest=digest_of({"case": selection_case}),
            ),
            TaskCase(
                case_id=final_case,
                workflow_id="W-C3",
                split=CaseSplit.FINAL_ACCEPTANCE,
                group_id="family-ax4-protocol",
                input_digest=digest_of({"case": final_case}),
            ),
        ),
        mode=RunMode.FIXTURE,
        notes="synthetic protocol promotion pack",
    )
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "W-C3")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    client = ProcessAdapterClient(
        [sys.executable, "-c", _CHILD], request_timeout_s=10, execute_timeout_s=30
    )
    run = controller.run_paired_evaluation(
        workflow_id="W-C3",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=client,
    )
    return store, artifacts, run, client


def _record_for(store: Any, artifacts: Any, run: Any, descriptor: Any) -> RunnerIntegrationRecord:
    from vouch_agent.contracts.evaluation import AttemptRecord

    attempt_digests = []
    revision = ""
    for attempt in run.attempts:
        stored = AttemptRecord.from_dict(store.load("attempt", attempt.attempt_id))
        attempt_digests.append(stored.digest())
        sealed = json.loads(artifacts.get(stored.output_digest).decode("utf-8"))
        revision = sealed["runnerVersion"]
    assert revision == RUNNER_ID, "the sealed exchanges must carry the child's revision"
    return RunnerIntegrationRecord(
        workflow_id="W-C3",
        runner_id=descriptor.adapter_id,
        runner_descriptor_digest=descriptor_digest(descriptor),
        verified_by="acceptance-owner",
        statement="promotion over the real framed protocol with sealed exchanges",
        runner_revision=revision,
        case_pack_digest=run.case_set_digest,
        coverage_layer=CoverageLayer.LOGIC,
        provider_mode="fixture",
        verified_run_id=run.run_id,
        verified_attempt_digests=tuple(attempt_digests),
        evidence_digest="sha256:" + "0" * 64,
    )


def test_real_protocol_run_promotes_reloads_and_reverifies() -> None:
    manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
    store, artifacts, run, client = _controller_with_protocol_run()
    try:
        descriptor = client.describe()  # the OBSERVED handshake descriptor
        record = _record_for(store, artifacts, run, descriptor)

        # promotion resolves the durable evidence produced over the protocol
        promoted = manifest.with_runner_integration(
            record, store=store, artifacts=artifacts, observed_descriptor=descriptor
        )
        assert promoted.effective_status("W-C3") == "runner-integrated"

        # reload: the persisted claim is unverified until re-resolution succeeds
        restored = WorkflowManifest.from_dict(json.loads(promoted.to_canonical_json()))
        assert restored.effective_status("W-C3") == "unverified-integration"
        assert restored.coverage_summary()["runner-integrated"] == []
        assert restored.coverage_summary()["unverified-integration"] == ["W-C3"]

        # re-resolution against the SAME durable protocol-produced state succeeds
        reverified = restored.verify_integrations(store, artifacts, descriptor)
        assert reverified.effective_status("W-C3") == "runner-integrated"
        assert reverified.coverage_summary()["runner-integrated"] == ["W-C3"]
        dimensions = reverified.coverage_dimensions()
        assert dimensions["W-C3"] == {
            "status": "runner-integrated",
            "layer": "logic",
            "mode": "fixture",
        }

        # and a re-resolution against an EMPTY store fails closed
        empty_store, empty_artifacts = FakeStore(), FakeArtifacts()
        try:
            reverified.verify_integrations(empty_store, empty_artifacts, descriptor)
            raise AssertionError("empty durable state must never verify an integration")
        except Exception as exc:
            assert "no durable evaluation run" in str(exc)
    finally:
        client.close()
