"""Shared contract primitives: canonical JSON, digests, run modes, roles, ids.

Every persisted contract serializes to canonical JSON (sorted keys, no
whitespace, UTF-8) and is content-addressed by ``sha256:<hex>`` of that
encoding. Digest stability is what makes approval invalidation work: an
approval binds a tuple of digests, and any byte-level change to a bound
object produces a new digest and thereby a different (invalid) binding.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from vouch_agent.errors import ContractError

DIGEST_PREFIX = "sha256:"


# JSON number domain shared with the TypeScript runner (protocol v1.1 §4):
#: safe integers only; fractional floats must be finite. Outside this domain
#: the two languages genuinely disagree on spelling, so digests cannot agree.
_SAFE_INT_MAX = 2**53 - 1


def _validate_digest_domain(obj: Any, path: str = "$") -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        if not -_SAFE_INT_MAX <= obj <= _SAFE_INT_MAX:
            raise ContractError(
                f"{path}: integer {obj} outside the safe-integer digest domain "
                f"(±{_SAFE_INT_MAX}); cross-language digests cannot agree on it"
            )
        return
    if isinstance(obj, float):
        import math

        if not math.isfinite(obj):
            raise ContractError(f"{path}: non-finite float {obj!r} is outside the digest domain")
        if obj.is_integer() and abs(obj) > _SAFE_INT_MAX:
            raise ContractError(
                f"{path}: integral float {obj!r} beyond the safe-integer digest "
                "domain; its JSON spelling disagrees across languages"
            )
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            _validate_digest_domain(value, f"{path}.{key}")
        return
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            _validate_digest_domain(value, f"{path}[{index}]")


def canonical_json(obj: Any) -> str:
    """Serialize to the canonical form used for all digests and persistence.

    Rejects values outside the shared cross-language digest domain: non-finite
    floats and integers beyond the JS safe range — their JSON spellings differ
    between Python and TypeScript, so digests over them could never agree
    (protocol v1.1 §4 / Choose runner test-vectors.json).
    """
    _validate_digest_domain(obj)
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_of(obj: Any) -> str:
    """Content digest over the canonical JSON encoding of ``obj``."""
    payload = canonical_json(obj).encode("utf-8")
    return DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def digest_bytes(payload: bytes) -> str:
    """Content digest for raw artifact bytes."""
    return DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def verify_digest(obj: Any, expected: str) -> None:
    from vouch_agent.errors import DigestMismatchError

    actual = digest_of(obj)
    if actual != expected:
        raise DigestMismatchError(f"digest mismatch: expected {expected}, got {actual}")


def utc_now_iso() -> str:
    """Aware UTC timestamp as ISO-8601 string — the only timestamp form in contracts."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    """New opaque id like ``cand_k62p...`` — ids are references, never secrets."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}{secrets.token_hex(4)}"


class RunMode(StrEnum):
    """Execution mode — §13.2 of the design doc; reports must never mix these.

    fixture: deterministic fake model/tools, network off, replay exhaustion is
    a terminal failure. Proves protocol and gating only, never model improvement.
    offline_evaluation: fixed materials + candidate; real model calls still
    cost money and must be metered.
    authorized_live: explicit task, data scope, model/tool path and budget.
    """

    FIXTURE = "fixture"
    OFFLINE_EVALUATION = "offline-evaluation"
    AUTHORIZED_LIVE = "authorized-live"

    def allows_live_calls(self) -> bool:
        return self is RunMode.AUTHORIZED_LIVE

    def allows_side_effects(self) -> bool:
        return self is RunMode.AUTHORIZED_LIVE

    def fail_closed_live(self) -> None:
        if not self.allows_live_calls():
            from vouch_agent.errors import LiveCallBlockedError

            raise LiveCallBlockedError(f"live calls are blocked in {self.value} mode")


class Role(StrEnum):
    """Responsibility roles (design §1.2). One human may hold several, never silently."""

    ENGINEER = "engineer"
    PROPOSER = "proposer"
    EVALUATOR = "evaluator"
    ACCEPTANCE_OWNER = "acceptance-owner"
    RELEASE_OWNER = "release-owner"
    BUDGET_OWNER = "budget-owner"


class ContractRecord:
    """Mixin for dataclass contracts: schema version, canonical JSON, digest.

    Subclasses are frozen dataclasses whose ``to_dict`` includes
    ``schemaVersion``; ``digest`` is derived and therefore excluded from the
    serialized form (it is recomputed on read and verified).
    """

    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def digest(self) -> str:
        return digest_of(self.to_dict())

    def to_json_file(self, path: Any) -> None:  # pragma: no cover - convenience
        from pathlib import Path

        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")

    @staticmethod
    def _check_version(data: dict[str, Any], expected: str = "1") -> None:
        version = data.get("schemaVersion")
        if version != expected:
            from vouch_agent.errors import UnknownVersionError

            raise UnknownVersionError(
                f"unsupported schemaVersion {version!r} (expected {expected!r})"
            )


def require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"field {field!r} must be a non-empty string, got {value!r}")
    return value


def require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX) or len(value) != 71:
        raise ContractError(f"field {field!r} must be a sha256 digest string, got {value!r}")
    return value
