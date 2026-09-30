"""SkillLedger — probation/trust/quarantine bookkeeping with reuse rights.

Five mechanisms (design §8): pre/post contracts, probation verification,
per-environment accounting, risk-graded reuse, drift re-check. States:
``distilled -> probation -> trusted``; a new environment, an aged entry or
detected drift returns the skill to probation; harmful outcomes quarantine
it with the failure evidence kept. There is no cross-customer universal
trust score, and no cross-project reuse without explicit authorization.
"""

from __future__ import annotations

from typing import Any

from vouch_agent.contracts.common import Role, digest_of, new_id, utc_now_iso
from vouch_agent.contracts.journal import EventKind, EventRecord
from vouch_agent.contracts.skill import (
    EnvironmentSignature,
    ReuseRight,
    SkillEntry,
    SkillState,
)
from vouch_agent.errors import ContractError, UnauthorizedReuseError
from vouch_agent.storage.interfaces import Journal, MetadataStore

KIND_SKILL = "skill"


class SkillLedger:
    def __init__(self, store: MetadataStore, journal: Journal) -> None:
        self.store = store
        self.journal = journal

    # --- write path ---------------------------------------------------------

    def _event(self, skill_id: str, data: dict[str, Any]) -> None:
        self.journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=EventKind.SKILL_STATE,
                subject=skill_id,
                data=data,
            )
        )

    def distill(
        self,
        *,
        content: str,
        environment: EnvironmentSignature,
        preconditions: tuple[str, ...] = (),
        postconditions: tuple[str, ...] = (),
        source: str = "internal",
    ) -> SkillEntry:
        """Record a new distilled increment. It carries NO reuse rights yet."""
        entry = SkillEntry(
            skill_id=new_id("skill"),
            content_digest=digest_of({"content": content}),
            environment=environment,
            preconditions=preconditions,
            postconditions=postconditions,
            state=SkillState.DISTILLED,
            reuse_right=ReuseRight.NONE,
            source=source,
        )
        self.store.save(KIND_SKILL, entry.skill_id, entry.to_dict())
        self._event(entry.skill_id, {"state": "distilled"})
        return entry

    def start_probation(
        self,
        skill_id: str,
        *,
        verification_refs: tuple[str, ...],
        reuse_right: ReuseRight = ReuseRight.PROJECT,
    ) -> SkillEntry:
        """distilled -> probation after verification evidence is attached.

        Rights are granted at probation the earliest, and only the scopes the
        verification actually covers (project, or one signed environment).
        """
        entry = self.get(skill_id)
        if not verification_refs:
            raise ContractError("probation requires at least one verification reference")
        moved = SkillEntry(
            skill_id=entry.skill_id,
            content_digest=entry.content_digest,
            environment=entry.environment,
            preconditions=entry.preconditions,
            postconditions=entry.postconditions,
            verification_refs=tuple(sorted(set(entry.verification_refs + verification_refs))),
            helpful=entry.helpful,
            harmful=entry.harmful,
            unknown=entry.unknown,
            state=SkillState.PROBATION,
            reuse_right=reuse_right,
            source=entry.source,
            first_seen_at=entry.first_seen_at,
            last_verified_at=utc_now_iso(),
        )
        self.store.save(KIND_SKILL, moved.skill_id, moved.to_dict())
        self._event(skill_id, {"state": "probation", "refs": list(verification_refs)})
        return moved

    def record_outcome(
        self,
        skill_id: str,
        *,
        outcome: str,
        environment: EnvironmentSignature | None = None,
    ) -> SkillEntry:
        """Account one use: helpful / harmful / unknown, per environment.

        A harmful outcome quarantines immediately (with evidence retained).
        An environment mismatch records `unknown` and degrades trust —
        matching contracts never guarantee applicability.
        """
        if outcome not in ("helpful", "harmful", "unknown"):
            raise ContractError(f"outcome must be helpful|harmful|unknown, got {outcome!r}")
        entry = self.get(skill_id)
        counts = {
            "helpful": entry.helpful,
            "harmful": entry.harmful,
            "unknown": entry.unknown,
        }
        counts[outcome] += 1
        updated = SkillEntry(
            skill_id=entry.skill_id,
            content_digest=entry.content_digest,
            environment=entry.environment,
            preconditions=entry.preconditions,
            postconditions=entry.postconditions,
            verification_refs=entry.verification_refs,
            helpful=counts["helpful"],
            harmful=counts["harmful"],
            unknown=counts["unknown"],
            state=entry.state,
            reuse_right=entry.reuse_right,
            source=entry.source,
            first_seen_at=entry.first_seen_at,
            last_verified_at=entry.last_verified_at,
        )
        if outcome == "harmful":
            updated = updated.with_transition(SkillState.QUARANTINED)
        elif (
            environment is not None
            and self._drifted(entry, environment)
            and entry.state is SkillState.TRUSTED
        ):
            updated = updated.with_transition(SkillState.PROBATION)
        self.store.save(KIND_SKILL, updated.skill_id, updated.to_dict())
        self._event(skill_id, {"outcome": outcome, "state": updated.state.value})
        return updated

    def promote(self, skill_id: str, *, verification_ref: str) -> SkillEntry:
        """probation -> trusted after independent verification (never self-verified)."""
        entry = self.get(skill_id)
        if entry.state is not SkillState.PROBATION:
            raise ContractError(
                f"only probation skills can be promoted (state={entry.state.value})"
            )
        promoted = SkillEntry(
            skill_id=entry.skill_id,
            content_digest=entry.content_digest,
            environment=entry.environment,
            preconditions=entry.preconditions,
            postconditions=entry.postconditions,
            verification_refs=tuple(sorted({*entry.verification_refs, verification_ref})),
            helpful=entry.helpful,
            harmful=entry.harmful,
            unknown=entry.unknown,
            state=SkillState.TRUSTED,
            reuse_right=entry.reuse_right,
            source=entry.source,
            first_seen_at=entry.first_seen_at,
            last_verified_at=utc_now_iso(),
        )
        self.store.save(KIND_SKILL, promoted.skill_id, promoted.to_dict())
        self._event(skill_id, {"state": "trusted", "verification": verification_ref})
        return promoted

    def degrade(self, skill_id: str, *, reason: str) -> SkillEntry:
        """trusted -> probation on drift/age; re-verification has its own budget."""
        entry = self.get(skill_id)
        degraded = entry.with_transition(SkillState.PROBATION)
        self.store.save(KIND_SKILL, degraded.skill_id, degraded.to_dict())
        self._event(skill_id, {"state": "probation", "reason": reason})
        return degraded

    # --- read path -----------------------------------------------------------

    def get(self, skill_id: str) -> SkillEntry:
        data = self.store.load(KIND_SKILL, skill_id)
        if data is None:
            raise ContractError(f"unknown skill {skill_id!r}")
        return SkillEntry.from_dict(data)

    def list_skills(self) -> list[SkillEntry]:
        skills: list[SkillEntry] = []
        for skill_id in self.store.list_ids(KIND_SKILL):
            data = self.store.load(KIND_SKILL, skill_id)
            if data is not None:
                skills.append(SkillEntry.from_dict(data))
        return skills

    def acquire(
        self,
        skill_id: str,
        environment: EnvironmentSignature,
        *,
        project_id: str,
        authorized_projects: frozenset[str],
        role: Role,
    ) -> SkillEntry:
        """Rights-before-reuse lookup for a consumer in a concrete environment.

        Refusals: quarantined/distilled entries, missing rights, project
        scope not authorized, environment signature mismatch for
        environment-authorized entries. Cross-project reads without
        authorization are the explicit attack this method must block.
        """
        entry = self.get(skill_id)
        project_authorized = project_id in authorized_projects
        entry.usable_in(environment, project_authorized=project_authorized)
        role_name = role.value if isinstance(role, Role) else str(role)
        self._event(
            skill_id,
            {
                "acquired-by": role_name,
                "project": project_id,
                "environment": digest_of(environment.to_dict()),
            },
        )
        return entry

    def health(self) -> dict[str, Any]:
        """Health accounting beyond hit-rate (design §8): verification success,
        fallbacks, harmful rate, coverage — so a single metric cannot drive
        over-reuse."""
        skills = self.list_skills()
        total = len(skills)
        by_state = {state.value: 0 for state in SkillState}
        helpful = harmful = unknown = 0
        for skill in skills:
            by_state[skill.state.value] += 1
            helpful += skill.helpful
            harmful += skill.harmful
            unknown += skill.unknown
        uses = helpful + harmful + unknown
        return {
            "total": total,
            "byState": by_state,
            "uses": uses,
            "helpfulRate": round(helpful / uses, 4) if uses else None,
            "harmfulRate": round(harmful / uses, 4) if uses else None,
            "unknownRate": round(unknown / uses, 4) if uses else None,
        }

    def _drifted(self, entry: SkillEntry, environment: EnvironmentSignature) -> bool:
        return digest_of(entry.environment.to_dict()) != digest_of(environment.to_dict())


class UnauthorizedReuseGuard:
    """Static helper for cross-project asset refusal at import time."""

    @staticmethod
    def assert_project_may_read(
        asset_project_id: str, reader_project_id: str, authorized: frozenset[str]
    ) -> None:
        if asset_project_id == reader_project_id:
            return
        if reader_project_id in authorized:
            return
        raise UnauthorizedReuseError(
            f"project {reader_project_id!r} is not authorized to read assets "
            f"from {asset_project_id!r}"
        )
