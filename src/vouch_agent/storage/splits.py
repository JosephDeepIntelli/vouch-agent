"""Role-aware task pack / case access (design §7.1 — data boundaries).

The four case splits are an *enforcement* boundary, not a convention:

* development — proposer-readable.
* selection-validation — controller-compared; usage counted into search history.
* final-acceptance — acceptance side only; the proposer can never read its
  labels, files or credentials, **regardless of which ids it names**.
* observation — post-release reporting.

Every read in this module goes through
:func:`vouch_agent.storage.interfaces.enforce_split_visibility` (or the
equivalent ``CaseSplit.readable_by`` filter for pack views), so a
proposer/engineer cannot reach final-acceptance data by constructing the
right id — naming the id still fails.

Known boundary (documented gap, not silently assumed away): the raw
:class:`~vouch_agent.storage.store.FileArtifactStore` is not role-aware. A
role that already holds *both* the final-acceptance input digest and the
artifact root could fetch bytes directly. The controller must therefore
provision artifact roots per role; :func:`load_case_input` enforces the
metadata path and is the sanctioned way to fetch case inputs.
"""

from __future__ import annotations

from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role
from vouch_agent.errors import ContractError, SplitAccessError
from vouch_agent.storage.interfaces import (
    ArtifactStore,
    MetadataStore,
    enforce_split_visibility,
)

TASK_PACK_KIND = "task-pack"
TASK_CASE_KIND = "task-case"


def _as_role(role: Role | str) -> Role:
    return role if isinstance(role, Role) else Role(role)


def save_task_case(store: MetadataStore, case: TaskCase) -> None:
    """Persist one case so it can be addressed (and gated) by case id."""
    store.save(TASK_CASE_KIND, case.case_id, case.to_dict())


def save_task_pack(store: MetadataStore, pack: TaskPack) -> None:
    """Persist a pack and each of its cases individually."""
    for case in pack.cases:
        save_task_case(store, case)
    store.save(TASK_PACK_KIND, pack.pack_id, pack.to_dict())


def load_task_pack(store: MetadataStore, pack_id: str, role: Role | str) -> TaskPack:
    """Load a pack as a role sees it.

    Cases in splits this role may not read are withheld from the returned
    view (the pack record itself only ever lists case data the role can see).
    If nothing readable remains, the whole pack is refused with
    :class:`SplitAccessError` — an empty "success" would quietly pretend the
    pack has no cases.
    """
    role_enum = _as_role(role)
    data = store.load(TASK_PACK_KIND, pack_id)
    if data is None:
        raise ContractError(f"unknown task pack {pack_id!r}")
    pack = TaskPack.from_dict(data)
    visible = tuple(
        case for case in pack.cases if CaseSplit(case.split.value).readable_by(role_enum.value)
    )
    if not visible:
        raise SplitAccessError(f"role {role_enum.value} may not read any case of pack {pack_id!r}")
    return TaskPack(
        pack_id=pack.pack_id,
        workflow_id=pack.workflow_id,
        cases=visible,
        source_snapshot_digest=pack.source_snapshot_digest,
        mode=pack.mode,
        notes=pack.notes,
    )


def load_case(store: MetadataStore, case_id: str, role: Role | str) -> TaskCase:
    """Load one case by id — the split gate applies to direct naming too.

    This is the attack surface the design calls out: a proposer that learned
    a final-acceptance case id still gets :class:`SplitAccessError`, because
    visibility is decided by the *stored* split, never by the caller's claim.
    """
    role_enum = _as_role(role)
    data = store.load(TASK_CASE_KIND, case_id)
    if data is None:
        raise ContractError(f"unknown task case {case_id!r}")
    case = TaskCase.from_dict(data)
    enforce_split_visibility(role_enum, case.split.value)
    return case


def load_case_input(
    store: MetadataStore, artifacts: ArtifactStore, case_id: str, role: Role | str
) -> bytes:
    """Fetch a case's input artifact through the split gate."""
    case = load_case(store, case_id, role)
    return artifacts.get(case.input_digest)


def assert_run_readable(split: CaseSplit | str, role: Role | str) -> None:
    """Gate a whole evaluation run by the split it was executed over.

    Runs over the final-acceptance split are acceptance-side material; a
    proposer/engineer asking to read such a run is refused even if it knows
    the run id.
    """
    split_value = split.value if isinstance(split, CaseSplit) else str(split)
    CaseSplit(split_value)  # validate
    enforce_split_visibility(_as_role(role), split_value)
