"""Split visibility attacks: proposer must not reach final-acceptance data."""

from __future__ import annotations

import pytest

from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role, digest_bytes, new_id
from vouch_agent.errors import ContractError, SplitAccessError
from vouch_agent.storage.splits import (
    assert_run_readable,
    load_case,
    load_case_input,
    load_task_pack,
    save_task_pack,
)
from vouch_agent.storage.store import FileArtifactStore, SqliteMetadataStore


def _case(split: CaseSplit, case_id: str, input_digest: str) -> TaskCase:
    return TaskCase(
        case_id=case_id,
        workflow_id="wf-compare",
        split=split,
        group_id="group-a",
        input_digest=input_digest,
    )


@pytest.fixture()
def packed_store(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    artifacts = FileArtifactStore(tmp_path)
    inputs = {split: artifacts.put(f"input-for-{split.value}".encode()) for split in CaseSplit}
    cases = [
        _case(CaseSplit.DEVELOPMENT, "case_dev_1", inputs[CaseSplit.DEVELOPMENT]),
        _case(CaseSplit.SELECTION_VALIDATION, "case_val_1", inputs[CaseSplit.SELECTION_VALIDATION]),
        _case(CaseSplit.FINAL_ACCEPTANCE, "case_fin_1", inputs[CaseSplit.FINAL_ACCEPTANCE]),
        _case(CaseSplit.FINAL_ACCEPTANCE, "case_fin_2", inputs[CaseSplit.FINAL_ACCEPTANCE]),
        _case(CaseSplit.OBSERVATION, "case_obs_1", inputs[CaseSplit.OBSERVATION]),
    ]
    pack = TaskPack(
        pack_id=new_id("pack"),
        workflow_id="wf-compare",
        cases=tuple(cases),
        source_snapshot_digest=digest_bytes(b"snapshot"),
    )
    save_task_pack(store, pack)
    return store, artifacts, pack


def test_proposer_pack_view_withholds_final_acceptance_and_observation(packed_store):
    store, _artifacts, pack = packed_store
    view = load_task_pack(store, pack.pack_id, Role.PROPOSER)
    assert [c.case_id for c in view.cases] == ["case_dev_1", "case_val_1"]
    assert all(
        c.split in (CaseSplit.DEVELOPMENT, CaseSplit.SELECTION_VALIDATION) for c in view.cases
    )


def test_engineer_sees_development_selection_and_observation_but_not_final(packed_store):
    store, _artifacts, pack = packed_store
    view = load_task_pack(store, pack.pack_id, Role.ENGINEER)
    assert [c.case_id for c in view.cases] == ["case_dev_1", "case_val_1", "case_obs_1"]


def test_acceptance_owner_and_evaluator_see_everything(packed_store):
    store, _artifacts, pack = packed_store
    for role in (Role.ACCEPTANCE_OWNER, Role.EVALUATOR):
        view = load_task_pack(store, pack.pack_id, role)
        assert len(view.cases) == 5


def test_attack_directly_naming_final_case_id_still_refused(packed_store):
    store, _artifacts, _pack = packed_store
    for role in (Role.PROPOSER, Role.ENGINEER):
        for case_id in ("case_fin_1", "case_fin_2"):
            with pytest.raises(SplitAccessError):
                load_case(store, case_id, role)
    # acceptance side can read them
    assert load_case(store, "case_fin_1", Role.ACCEPTANCE_OWNER).case_id == "case_fin_1"
    assert load_case(store, "case_fin_2", Role.EVALUATOR).case_id == "case_fin_2"


def test_attack_proposer_case_input_fetch_refused(packed_store):
    store, artifacts, _pack = packed_store
    with pytest.raises(SplitAccessError):
        load_case_input(store, artifacts, "case_fin_1", Role.PROPOSER)
    payload = load_case_input(store, artifacts, "case_fin_1", Role.ACCEPTANCE_OWNER)
    assert payload == b"input-for-final-acceptance"


def test_pack_entirely_final_acceptance_is_refused_for_proposer(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    artifacts = FileArtifactStore(tmp_path)
    digest = artifacts.put(b"secret-input")
    pack = TaskPack(
        pack_id=new_id("pack"),
        workflow_id="wf-compare",
        cases=(_case(CaseSplit.FINAL_ACCEPTANCE, "case_only_fin", digest),),
    )
    save_task_pack(store, pack)
    with pytest.raises(SplitAccessError):
        load_task_pack(store, pack.pack_id, Role.PROPOSER)


def test_unknown_pack_and_case_ids_fail_closed(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    with pytest.raises(ContractError, match="unknown task pack"):
        load_task_pack(store, "pack_nope", Role.EVALUATOR)
    with pytest.raises(ContractError, match="unknown task case"):
        load_case(store, "case_nope", Role.EVALUATOR)


def test_roles_accepted_as_strings(packed_store):
    store, _artifacts, pack = packed_store
    view = load_task_pack(store, pack.pack_id, "proposer")
    assert [c.case_id for c in view.cases] == ["case_dev_1", "case_val_1"]


def test_run_level_gate_for_final_acceptance_runs():
    assert_run_readable(CaseSplit.FINAL_ACCEPTANCE, Role.EVALUATOR)  # no raise
    for role in (Role.PROPOSER, Role.ENGINEER):
        with pytest.raises(SplitAccessError):
            assert_run_readable("final-acceptance", role)
    # development runs stay proposer-readable
    assert_run_readable(CaseSplit.DEVELOPMENT, Role.PROPOSER)  # no raise


def test_saved_pack_record_is_untouched_by_role_views(packed_store):
    store, _artifacts, pack = packed_store
    load_task_pack(store, pack.pack_id, Role.PROPOSER)  # filtered view
    stored = store.load("task-pack", pack.pack_id)
    assert stored is not None and len(stored["cases"]) == 5
