"""Synthetic task-pack construction from the in-repo Choose fixture pack.

The fixture pack under ``fixtures/choose/`` is explicitly synthetic (every
scenario carries ``"synthetic": true`` and the loader refuses anything else).
This module turns a workflow's fixture scenarios into a real
:class:`~vouch_agent.contracts.cases.TaskPack`:

* case id = scenario id (the fixture adapter replays by scenario id),
* case inputs are content-addressed into the project artifact store,
* cases are split across development / selection-validation / final-acceptance
  so the paired-evaluation and acceptance splits both have material,
* the workflow manifest (declaration vs fixture coverage) is persisted for the
  review/coverage report.

Nothing here is evidence of model or product improvement; the generated pack
exists to exercise the pipeline offline (design §3.1, §13.2).
"""

from __future__ import annotations

from pathlib import Path

from vouch_agent.adapters.fixture_adapter import FixturePack
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role, RunMode, canonical_json, digest_of
from vouch_agent.errors import ContractError
from vouch_agent.workflows import WorkflowManifest

#: Record kinds this module writes (in addition to the pack/case kinds).
MANIFEST_KIND = "workflow-manifest"


def _case_input_payload(scenario_id: str, case: dict) -> dict:
    return {"schemaVersion": "1", "scenario": scenario_id, "case": case}


def import_fixture_pack(
    workspace: ProjectWorkspace,
    fixtures_dir: Path,
    workflow_id: str,
    *,
    dev: int = 2,
    selection: int = 0,
    final: int = 2,
    pack_id: str | None = None,
    role: Role = Role.EVALUATOR,
) -> TaskPack:
    """Build + import a synthetic TaskPack for ``workflow_id`` from fixtures.

    ``dev`` scenarios go to the development split, ``selection`` to
    selection-validation, ``final`` to final acceptance, taken in the pack's
    declared fixture order. Fails closed when the requested counts exceed the
    available scenarios or the workflow is not covered.
    """
    pack = FixturePack.load(fixtures_dir)
    coverage = pack.workflow_coverage.get(workflow_id)
    if coverage is None:
        raise ContractError(
            f"fixture pack {pack.pack_id!r} does not cover workflow {workflow_id!r}; "
            f"covered: {sorted(pack.workflow_coverage)}"
        )
    fixture_ids = list(coverage.get("fixtureIds", []))
    wanted = dev + selection + final
    if wanted == 0:
        raise ContractError("pack needs at least one case (dev/selection/final all 0)")
    if wanted > len(fixture_ids):
        raise ContractError(
            f"workflow {workflow_id!r} has {len(fixture_ids)} fixture scenario(s); "
            f"{wanted} requested (dev={dev}, selection={selection}, final={final})"
        )

    cases: list[TaskCase] = []
    cursor = 0
    for count, split in (
        (dev, CaseSplit.DEVELOPMENT),
        (selection, CaseSplit.SELECTION_VALIDATION),
        (final, CaseSplit.FINAL_ACCEPTANCE),
    ):
        for offset in range(count):
            scenario_id = fixture_ids[cursor + offset]
            scenario = pack.scenarios[scenario_id]
            payload = _case_input_payload(scenario_id, scenario.case)
            input_digest = digest_of(payload)
            # Content-address the synthetic input so case reads verify by digest.
            workspace.artifacts.put(canonical_json(payload).encode("utf-8"))
            cases.append(
                TaskCase(
                    case_id=scenario_id,
                    workflow_id=workflow_id,
                    split=split,
                    group_id=f"synthetic-{workflow_id}",
                    input_digest=input_digest,
                    source_refs=(scenario_id,),
                    locale=scenario.locale,
                    market=scenario.market,
                    synthetic=True,
                )
            )
        cursor += count

    task_pack = TaskPack(
        pack_id=pack_id or f"{workflow_id.lower()}-synthetic",
        workflow_id=workflow_id,
        cases=tuple(cases),
        mode=RunMode.FIXTURE,
        notes=(
            f"SYNTHETIC pack generated from fixture pack {pack.pack_id!r} "
            f"({fixtures_dir}); proves the pipeline only, never improvement"
        ),
    )
    from vouch_agent.storage import save_task_pack

    save_task_pack(workspace.store, task_pack)
    workspace.controller().import_pack(task_pack, role)

    manifest = WorkflowManifest.from_fixture_pack(pack)
    workspace.store.save(MANIFEST_KIND, pack.pack_id, manifest.to_dict())
    return task_pack


def load_manifest(workspace: ProjectWorkspace) -> WorkflowManifest | None:
    """The manifest persisted by the last fixture-pack import, if any."""
    ids = workspace.store.list_ids(MANIFEST_KIND)
    if not ids:
        return None
    data = workspace.store.load(MANIFEST_KIND, ids[0])
    if data is None:  # pragma: no cover - id came from list_ids
        return None
    return WorkflowManifest.from_dict(data)


def import_choose_application_pack(
    workspace: ProjectWorkspace,
    *,
    split: CaseSplit,
    role: Role = Role.EVALUATOR,
) -> TaskPack:
    """Import the Choose runner's APPLICATION-scope case as a TaskPack.

    Talks to the REAL runner (describe handshake only) and imports exactly the
    case its apply-config operation executes, in the requested split — so the
    public improvement journey's pack case identity is the runner's own, not a
    Vouch fixture vocabulary the runner would refuse mid-evaluation. One case
    per pack (TaskPack case ids are unique); import once per split.

    SYNTHETIC, fixture-mode: proves application and decision plumbing only,
    never model improvement.
    """
    from vouch_agent.adapters.process_adapter import DEFAULT_EXECUTE_TIMEOUT_S, ProcessAdapterClient
    from vouch_agent.appservices.choose_apply import evaluation_cases_from_describe
    from vouch_agent.appservices.flow import configured_choose_repo

    repo = configured_choose_repo()
    tsx = repo / "node_modules" / ".bin" / "tsx"
    if not tsx.is_file() or not (repo / "scripts" / "vouch" / "runner.ts").is_file():
        raise ContractError(
            f"the Choose runner is not available at {repo} (no node_modules/.bin/tsx "
            "or no scripts/vouch/runner.ts); set VOUCH_CHOOSE_RUNNER_DIR to a "
            "prepared Choose checkout"
        )
    inner = ProcessAdapterClient(
        [str(tsx), "scripts/vouch/runner.ts"],
        cwd=str(repo),
        execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
    )
    try:
        inner.describe()
        cases = evaluation_cases_from_describe(inner.describe_payload)
    finally:
        inner.close()
    applicable = sorted(
        (case for case in cases.values() if case.application),
        key=lambda case: case.case_id,
    )
    if not applicable:
        raise ContractError(
            "the Choose runner advertises no application-capable case; the "
            "improvement flow cannot import an application pack"
        )
    entry = applicable[0]
    payload = {
        "schemaVersion": "1",
        "caseId": entry.case_id,
        "workflowId": entry.workflow_id,
        "source": "choose-runner-apply-config",
        "application": True,
    }
    input_digest = digest_of(payload)
    workspace.artifacts.put(canonical_json(payload).encode("utf-8"))
    case = TaskCase(
        case_id=entry.case_id,
        workflow_id=entry.workflow_id,
        split=split,
        group_id=f"choose-application-{entry.workflow_id.lower()}",
        input_digest=input_digest,
        source_refs=(entry.case_id,),
        locale=entry.locale or "en",
        market=entry.market or "US",
        synthetic=True,
    )
    task_pack = TaskPack(
        pack_id=f"choose-application-{split.value}",
        workflow_id=entry.workflow_id,
        cases=(case,),
        mode=RunMode.FIXTURE,
        notes=(
            "SYNTHETIC application-scope pack: the Choose runner's own "
            f"apply-config case ({entry.case_id}, {entry.workflow_id}); baseline "
            "and candidate attempts both travel the application path. Proves "
            "application and decision plumbing only, never model improvement."
        ),
    )
    from vouch_agent.storage import save_task_pack

    save_task_pack(workspace.store, task_pack)
    workspace.controller().import_pack(task_pack, role)
    return task_pack
