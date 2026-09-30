"""ProcessAdapterClient end-to-end tests over the real fixture adapter subprocess.

Every workflow family W-C1..W-C9 walks the full describe -> prepare ->
execute -> collect -> cleanup cycle, plus en/zh and cross-market execution,
determinism, revision/handoff/session journeys, failure paths (insufficient
evidence, exhaustion, cancel, overrun-kill) and client-side frame validation
against deliberately broken child adapters.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from vouch_agent.adapters.process_adapter import ProcessAdapterClient, fixture_adapter_command
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import AdapterExecutionError, MissingMeteringError, ProtocolFrameError

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "choose"

#: One representative scenario per declared workflow family.
FAMILY_CYCLES = [
    ("W-C1", "prepare-normal-en-us", {"freeText": "a quieter vacuum"}),
    ("W-C2", "find-normal-en-us", {}),
    ("W-C3", "compare-normal-zh-cn", {}),
    ("W-C4", "setup-normal-en-us", {}),
    ("W-C5", "revision-compare-change-product-en-us", {"parent": {"version": 2}}),
    (
        "W-C6",
        "handoff-find-to-compare-en-us",
        {"handoff": {"from": "find", "selected": ["a", "c"]}},
    ),
    ("W-C7", "session-save-reopen-resume-zh-cn", {"step": "save"}),
    ("W-C8", "fail-insufficient-evidence-en-us", {}),
    ("W-C9", "guard-credit-hold-release-en-us", {}),
]


def make_client(tmp_path: Path, **kwargs: object) -> ProcessAdapterClient:
    return ProcessAdapterClient(
        fixture_adapter_command(str(FIXTURES), str(tmp_path / "workspace")),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(("workflow_id", "scenario", "extra_input"), FAMILY_CYCLES)
def test_full_cycle_per_workflow_family(
    tmp_path: Path, workflow_id: str, scenario: str, extra_input: dict
) -> None:
    with make_client(tmp_path) as client:
        descriptor = client.describe()
        assert workflow_id in descriptor.workflows
        assert descriptor.enforced_modes == (RunMode.FIXTURE,)

        run_id = f"run_{workflow_id}"
        client.prepare(run_id, RunMode.FIXTURE)
        execution = client.execute(
            run_id=run_id,
            attempt_id="att_1",
            workflow_id=workflow_id,
            case_input={"scenario": scenario, **extra_input},
            mode=RunMode.FIXTURE,
        )
        assert execution.ok is True, execution.error
        assert execution.mode is RunMode.FIXTURE
        assert execution.runner_version.startswith("fixture-choose-v1/")
        assert execution.usage is not None and execution.usage["modelSteps"] >= 1
        assert execution.outputs["synthetic"] is True
        assert execution.outputs["workflowId"] == workflow_id
        assert execution.evidence_refs, "execute must seal evidence"

        digests = client.collect(run_id)
        assert list(execution.evidence_refs) == list(digests)
        client.cleanup(run_id)
        # a cleaned-up run cannot be collected again: the client refuses
        # BEFORE any frame is sent (A6 — collect is bound to the prepared run)
        with pytest.raises(AdapterExecutionError, match="never prepared on this adapter client"):
            client.collect(run_id)


def test_revision_preserves_parent_answer(tmp_path: Path) -> None:
    parent = {"version": 2, "outcome": "complete", "reportDigest": "sha256:" + "9" * 64}
    with make_client(tmp_path) as client:
        client.prepare("run_rev", RunMode.FIXTURE)
        ok = client.execute(
            run_id="run_rev",
            attempt_id="a1",
            workflow_id="W-C5",
            case_input={
                "scenario": "revision-compare-change-product-en-us",
                "parent": parent,
                "change": {"candidates[1]": "Synthetic Model D"},
            },
            mode=RunMode.FIXTURE,
        )
        assert ok.outputs["parentAnswerPreserved"] is True
        assert ok.outputs["childVersion"] == 3
        assert ok.outputs["parentOutcome"] == "complete"

        failed = client.execute(
            run_id="run_rev",
            attempt_id="a2",
            workflow_id="W-C5",
            case_input={
                "scenario": "revision-failed-preserves-parent-zh-cn",
                "parent": parent,
                "change": {"candidates[0]": ""},
            },
            mode=RunMode.FIXTURE,
        )
        assert failed.ok is False
        assert failed.error == "choose/revision-failed"
        assert failed.outputs["parentAnswerPreserved"] is True
        assert failed.usage is not None and failed.usage["creditsDebited"] == 0


def test_handoff_inherits_selection_but_not_credentials(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.prepare("run_hand", RunMode.FIXTURE)
        prefill = client.execute(
            run_id="run_hand",
            attempt_id="a1",
            workflow_id="W-C6",
            case_input={
                "scenario": "handoff-find-to-compare-en-us",
                "handoff": {"from": "find", "selected": ["Synthetic Model C", "Synthetic Model A"]},
            },
            mode=RunMode.FIXTURE,
        )
        assert prefill.outputs["inheritedSelection"] == ["Synthetic Model C", "Synthetic Model A"]
        assert prefill.outputs["credentialsInherited"] is False

        exclusion = client.execute(
            run_id="run_hand",
            attempt_id="a2",
            workflow_id="W-C6",
            case_input={
                "scenario": "handoff-compare-reject-to-find-zh-cn",
                "handoff": {"from": "compare", "rejected": ["Synthetic Model B"]},
                "exclusions": ["Synthetic Model B"],
            },
            mode=RunMode.FIXTURE,
        )
        assert exclusion.outputs["exclusionsApplied"] == ["Synthetic Model B"]
        assert exclusion.outputs["exclusionsAreInstructionsNotOptions"] is True


def test_session_save_reopen_resume_journey(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        run_id = "run_sess"
        client.prepare(run_id, RunMode.FIXTURE)
        common = {"scenario": "session-save-reopen-resume-zh-cn"}
        saved = client.execute(
            run_id=run_id,
            attempt_id="s1",
            workflow_id="W-C7",
            case_input={**common, "step": "save", "draft": {"purpose": "轻便吸尘器"}},
            mode=RunMode.FIXTURE,
        )
        assert saved.outputs["saved"] is True
        reopened = client.execute(
            run_id=run_id,
            attempt_id="s2",
            workflow_id="W-C7",
            case_input={**common, "step": "reopen"},
            mode=RunMode.FIXTURE,
        )
        assert reopened.outputs["draftRestored"] == {"purpose": "轻便吸尘器"}
        resumed = client.execute(
            run_id=run_id,
            attempt_id="s3",
            workflow_id="W-C7",
            case_input={**common, "step": "resume"},
            mode=RunMode.FIXTURE,
        )
        assert resumed.outputs["resumed"] is True
        assert resumed.outputs["outcome"] == "complete"


def test_locale_switch_preserves_report_language(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        run_id = "run_loc"
        client.prepare(run_id, RunMode.FIXTURE)
        base = {"scenario": "session-locale-switch-en-us"}
        client.execute(
            run_id=run_id,
            attempt_id="l1",
            workflow_id="W-C7",
            case_input={**base, "step": "save"},
            mode=RunMode.FIXTURE,
        )
        switched = client.execute(
            run_id=run_id,
            attempt_id="l2",
            workflow_id="W-C7",
            case_input={**base, "step": "reopen-locale-switched", "uiLocale": "zh"},
            mode=RunMode.FIXTURE,
        )
        assert switched.outputs["uiLocale"] == "zh"
        assert switched.outputs["reportLanguagePreserved"] == "en"


@pytest.mark.parametrize(
    ("workflow_id", "scenario", "case_input", "locale", "market", "currency"),
    [
        ("W-C2", "find-normal-en-us", {}, "en", "US", "USD"),
        ("W-C2", "find-ambiguous-question-zh-cn", {}, "zh", "CN", "CNY"),
        ("W-C3", "compare-normal-zh-cn", {}, "zh", "CN", "CNY"),
        ("W-C3", "compare-adverse-blocked-partial-en-us", {}, "en", "US", "USD"),
        ("W-C1", "prepare-clarify-budget-currency-zh-cn", {}, "zh", "US", "USD"),
        ("W-C7", "session-locale-switch-en-us", {"step": "save"}, "en", "CN", "CNY"),
        ("W-C9", "guard-storage-failure-zh-cn", {}, "zh", "US", "USD"),
    ],
)
def test_locale_market_currency_echo(
    tmp_path: Path,
    workflow_id: str,
    scenario: str,
    case_input: dict,
    locale: str,
    market: str,
    currency: str,
) -> None:
    with make_client(tmp_path) as client:
        run_id = "run_l10n"
        client.prepare(run_id, RunMode.FIXTURE)
        execution = client.execute(
            run_id=run_id,
            attempt_id="a1",
            workflow_id=workflow_id,
            case_input={"scenario": scenario, **case_input},
            mode=RunMode.FIXTURE,
        )
        assert execution.outputs["locale"] == locale
        assert execution.outputs["market"] == market
        assert execution.outputs["currency"] == currency
        assert execution.usage is not None


def test_deterministic_replay_same_outputs_and_digests(tmp_path: Path) -> None:
    results = []
    for i in range(2):
        with make_client(tmp_path) as client:
            run_id = f"run_det_{i}"
            client.prepare(run_id, RunMode.FIXTURE)
            execution = client.execute(
                run_id=run_id,
                attempt_id="a1",
                workflow_id="W-C3",
                case_input={"scenario": "compare-normal-zh-cn"},
                mode=RunMode.FIXTURE,
            )
            results.append((execution.outputs, execution.usage, client.collect(run_id)))
    assert results[0] == results[1]


def test_insufficient_evidence_is_honest_unbilled_partial(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.prepare("run_ins", RunMode.FIXTURE)
        execution = client.execute(
            run_id="run_ins",
            attempt_id="a1",
            workflow_id="W-C8",
            case_input={"scenario": "fail-insufficient-evidence-en-us"},
            mode=RunMode.FIXTURE,
        )
        assert execution.ok is True  # a saved insufficient answer is a terminal state
        assert execution.outputs["outcome"] == "insufficient_sources"
        assert execution.outputs["billed"] is False
        assert execution.outputs["sources"] == []
        assert execution.usage is not None and execution.usage["creditsDebited"] == 0


def test_budget_exhaustion_saves_limited_answer(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.prepare("run_exh", RunMode.FIXTURE)
        execution = client.execute(
            run_id="run_exh",
            attempt_id="a1",
            workflow_id="W-C8",
            case_input={"scenario": "fail-exhausted-budget-en-us"},
            mode=RunMode.FIXTURE,
        )
        assert execution.outputs["outcome"] == "exhausted"
        assert execution.outputs["billed"] is False
        assert execution.outputs["noEndlessResumeLoop"] is True
        assert execution.usage is not None and execution.usage["creditsDebited"] == 0


def test_cancelled_run_releases_hold(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.prepare("run_cxl", RunMode.FIXTURE)
        execution = client.execute(
            run_id="run_cxl",
            attempt_id="a1",
            workflow_id="W-C8",
            case_input={"scenario": "fail-cancel-en-us"},
            mode=RunMode.FIXTURE,
        )
        assert execution.ok is False
        assert execution.error == "choose/cancelled"
        assert execution.outputs["holdReleased"] is True
        assert execution.usage is not None and execution.usage["creditsDebited"] == 0


def test_overrun_kills_subprocess_and_fails_closed(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.prepare("run_stall", RunMode.FIXTURE)
    with pytest.raises(AdapterExecutionError, match="overrun"):
        client.execute(
            run_id="run_stall",
            attempt_id="a1",
            workflow_id="W-C8",
            case_input={"scenario": "fail-stall-timeout-zh-cn"},
            mode=RunMode.FIXTURE,
            timeout_s=1.0,
        )
    assert client.dead is True
    assert "stalling" in client.diagnostics()
    # fail closed: no further work on a killed client, no silent respawn
    with pytest.raises(AdapterExecutionError, match="recreate the client"):
        client.execute(
            run_id="run_stall",
            attempt_id="a2",
            workflow_id="W-C8",
            case_input={"scenario": "fail-cancel-en-us"},
            mode=RunMode.FIXTURE,
        )
    client.close()


def test_non_fixture_mode_refused_by_adapter(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        with pytest.raises(AdapterExecutionError, match="vouch/mode-not-enforced"):
            client.prepare("run_live", RunMode.AUTHORIZED_LIVE)


def test_unknown_scenario_and_mismatch_surfaced(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.prepare("run_bad", RunMode.FIXTURE)
        with pytest.raises(AdapterExecutionError, match="fixture/unknown-scenario"):
            client.execute(
                run_id="run_bad",
                attempt_id="a1",
                workflow_id="W-C2",
                case_input={"scenario": "does-not-exist"},
                mode=RunMode.FIXTURE,
            )
        with pytest.raises(AdapterExecutionError, match="fixture/workflow-mismatch"):
            client.execute(
                run_id="run_bad",
                attempt_id="a2",
                workflow_id="W-C4",
                case_input={"scenario": "find-normal-en-us"},
                mode=RunMode.FIXTURE,
            )


# --- deliberately broken child adapters (client-side validation attacks) ------


def _write_child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


GOOD_DESCRIBE = """\
    import json, sys
    line = sys.stdin.buffer.readline()
    sys.stdout.buffer.write(json.dumps({
        "protocolVersion": "1", "seq": 0, "kind": "describe-response",
        "runId": None,
        "payload": {"adapterId": "broken-child", "protocolVersion": "1",
                    "workflows": ["W-C2"], "actions": ["describe", "execute"],
                    "enforcedModes": ["fixture"], "notes": ""},
    }).encode() + b"\\n")
    sys.stdout.buffer.flush()
    line = sys.stdin.buffer.readline()
    sys.stdout.buffer.write(json.dumps({
        "protocolVersion": "1", "seq": 1, "kind": "prepare-response",
        "runId": "run_1", "payload": {"ok": True}}).encode() + b"\\n")
    sys.stdout.buffer.flush()
"""


def test_child_with_wrong_version_rejected(tmp_path: Path) -> None:
    command = _write_child(
        tmp_path,
        """
        import json, sys
        sys.stdin.buffer.readline()
        sys.stdout.buffer.write(json.dumps({
            "protocolVersion": "2", "seq": 0, "kind": "describe-response",
            "runId": None, "payload": {}}).encode() + b"\\n")
        sys.stdout.buffer.flush()
        """,
    )
    client = ProcessAdapterClient(command)
    with pytest.raises(ProtocolFrameError, match="unknown protocolVersion"):
        client.describe()


def test_child_with_seq_gap_rejected(tmp_path: Path) -> None:
    command = _write_child(
        tmp_path,
        GOOD_DESCRIBE.replace('"seq": 0', '"seq": 7'),
    )
    client = ProcessAdapterClient(command)
    with pytest.raises(ProtocolFrameError, match="out-of-order"):
        client.describe()


def test_child_without_metering_rejected(tmp_path: Path) -> None:
    command = _write_child(
        tmp_path,
        GOOD_DESCRIBE
        + """\
    line = sys.stdin.buffer.readline()
    sys.stdout.buffer.write(json.dumps({
        "protocolVersion": "1", "seq": 2, "kind": "execute-response",
        "runId": "run_1",
        "payload": {"ok": True, "outputs": {}, "usage": None}}).encode() + b"\\n")
    sys.stdout.buffer.flush()
    """,
    )
    client = ProcessAdapterClient(command)
    client.describe()
    client.prepare("run_1", RunMode.FIXTURE)
    with pytest.raises(MissingMeteringError):
        client.execute(
            run_id="run_1",
            attempt_id="a1",
            workflow_id="W-C2",
            case_input={"scenario": "find-normal-en-us"},
            mode=RunMode.FIXTURE,
        )


def test_child_exiting_early_surfaces_diagnostics(tmp_path: Path) -> None:
    command = _write_child(
        tmp_path,
        """
        import sys
        sys.stdin.buffer.readline()
        sys.stderr.write("api_key=DUMMY boot failed\\n")
        sys.stderr.flush()
        sys.exit(3)
        """,
    )
    client = ProcessAdapterClient(command)
    with pytest.raises(AdapterExecutionError, match="exited"):
        client.describe()
    diagnostics = client.diagnostics()
    assert "DUMMY" not in diagnostics
    assert "[redacted]" in diagnostics
