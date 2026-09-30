"""Material snapshot helpers (M4 A3; ownership: evidence specialist).

Two rules the native task path must keep when it turns operator-supplied
files into task materials:

* **one digest per material.** Each input snapshot is stored under its OWN
  content digest. The previous shape concatenated every input into one blob
  (``left + b"\\x00" + right``) and referenced that single digest — a
  concatenated blob cannot satisfy references to two separate materials, and
  a change to one input silently invalidated the shared reference.

* **internal attachment ids are not filenames.** The durable
  :class:`~vouch_agent.contracts.materials.TaskAttachment` name is a safe
  generated identifier (the contract restricts it to
  ``[A-Za-z0-9._-]{1,64}``); the USER-VISIBLE filename — spaces, Chinese
  characters and all — travels beside it as ``display_name`` and is what
  reports and exports show. Filenames therefore never have to be mangled
  into validity, and two files may keep distinct display names even when
  their bytes digest identically.

The application service (``appservices/execution.py``, runtime ownership)
wires these helpers at its call sites; this module deliberately depends only
on contracts + the artifact store port so it stays wiring-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vouch_agent.contracts.common import require_digest, require_str
from vouch_agent.contracts.materials import MAX_MATERIAL_BYTES, MaterialKind, TaskAttachment
from vouch_agent.errors import ContractError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vouch_agent.storage.interfaces import ArtifactStore

#: User-visible filenames are bounded too (they are rendered everywhere).
MAX_DISPLAY_NAME_CHARS = 200
#: Path separators and NUL are never legitimate in a bare filename.
_FORBIDDEN_NAME_CHARS = ("/", "\\", "\x00")


def validate_display_name(name: str) -> str:
    """A user-visible filename: any UTF-8, but bounded and path-safe."""
    require_str(name, "displayName")
    if not name.strip():
        raise ContractError("display name must not be empty or whitespace-only")
    if len(name) > MAX_DISPLAY_NAME_CHARS:
        raise ContractError(
            f"display name {name[:32]!r}... exceeds {MAX_DISPLAY_NAME_CHARS} characters"
        )
    for character in _FORBIDDEN_NAME_CHARS:
        if character in name:
            raise ContractError(
                f"display name {name!r} contains {character!r}; pass a bare file name, "
                "not a path"
            )
    if any(ord(character) < 0x20 for character in name):
        raise ContractError(f"display name {name!r} contains control characters")
    return name


def safe_attachment_id(ordinal: int, digest: str, *, prefix: str = "mat") -> str:
    """The internal attachment id: safe by construction, collision-safe
    against display names, and derived from the material's own digest."""
    require_digest(digest, "digest")
    if not 0 <= ordinal <= 9999:
        raise ContractError(f"material ordinal {ordinal} outside 0..9999")
    if not prefix.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ContractError(f"attachment id prefix {prefix!r} must be [A-Za-z0-9._-]")
    identifier = f"{prefix}-{ordinal:02d}-{digest.removeprefix('sha256:')[:16]}"
    if len(identifier) > 64:
        raise ContractError(f"generated attachment id {identifier!r} exceeds 64 chars")
    return identifier


@dataclass(frozen=True)
class MaterialSnapshot:
    """One immutable input material, ready to be stored and referenced.

    ``attachment.name`` is the SAFE INTERNAL ID (see :func:`safe_attachment_id`);
    ``display_name`` is the user-visible filename. ``digest`` is this
    material's OWN content digest — the reference callers may cite.
    """

    attachment: TaskAttachment
    display_name: str
    payload: bytes

    @property
    def digest(self) -> str:
        return self.attachment.content_digest

    def to_input_record(self) -> dict[str, Any]:
        """The durable record for ``TaskSpec.inputs["materials"]``: the
        attachment (id, kind, digest) plus the display name beside it."""
        return {
            "schemaVersion": "1",
            "attachmentId": self.attachment.name,
            "displayName": self.display_name,
            "attachment": self.attachment.to_dict(),
        }


def _snapshot(
    payload: bytes,
    *,
    display_name: str,
    ordinal: int,
    kind: MaterialKind,
    media_type: str,
    csv_config: dict[str, Any] | None,
) -> MaterialSnapshot:
    from vouch_agent.contracts.common import digest_bytes

    validate_display_name(display_name)
    if len(payload) > MAX_MATERIAL_BYTES:
        raise ContractError(
            f"material {display_name!r} exceeds the {MAX_MATERIAL_BYTES}-byte cap "
            f"({len(payload)} bytes)"
        )
    digest = digest_bytes(payload)
    attachment = TaskAttachment(
        name=safe_attachment_id(ordinal, digest),
        kind=kind,
        media_type=media_type,
        content_digest=digest,
        size_bytes=len(payload),
        csv_config=dict(csv_config or {}),
    )
    return MaterialSnapshot(
        attachment=attachment, display_name=display_name, payload=payload
    )


def snapshot_csv_material(
    payload: bytes,
    *,
    display_name: str,
    ordinal: int,
    delimiter: str,
    csv_config: dict[str, Any] | None = None,
) -> MaterialSnapshot:
    """Snapshot a CSV material (delimiter is part of its identity)."""
    config: dict[str, Any] = {"delimiter": delimiter, "header": True}
    config.update(csv_config or {})
    return _snapshot(
        payload,
        display_name=display_name,
        ordinal=ordinal,
        kind=MaterialKind.CSV,
        media_type="text/csv",
        csv_config=config,
    )


def snapshot_json_material(
    payload: bytes, *, display_name: str, ordinal: int
) -> MaterialSnapshot:
    return _snapshot(
        payload,
        display_name=display_name,
        ordinal=ordinal,
        kind=MaterialKind.JSON,
        media_type="application/json",
        csv_config=None,
    )


def snapshot_text_material(
    payload: bytes, *, display_name: str, ordinal: int
) -> MaterialSnapshot:
    return _snapshot(
        payload,
        display_name=display_name,
        ordinal=ordinal,
        kind=MaterialKind.TEXT,
        media_type="text/plain; charset=utf-8",
        csv_config=None,
    )


def store_material_snapshots(
    artifacts: ArtifactStore, snapshots: tuple[MaterialSnapshot, ...] | list[MaterialSnapshot]
) -> tuple[str, ...]:
    """Store EVERY snapshot under its OWN digest and return the per-material
    digests, in input order.

    The returned digests are individually citable (e.g. as a ResultPackage's
    ``artifact_refs``): each material's bytes verify against its own name,
    and removing or changing one input never disturbs another's reference.
    """
    ids = {snapshot.attachment.name for snapshot in snapshots}
    if len(ids) != len(snapshots):
        raise ContractError("material attachment ids collided; ordinals must be unique")
    digests: list[str] = []
    for snapshot in snapshots:
        stored = artifacts.put(snapshot.payload)
        if stored != snapshot.digest:  # pragma: no cover - put digests what it stores
            raise ContractError(
                f"artifact store returned {stored} for material "
                f"{snapshot.display_name!r} whose bytes digest to {snapshot.digest}"
            )
        digests.append(stored)
    return tuple(digests)


def material_input_records(
    snapshots: tuple[MaterialSnapshot, ...] | list[MaterialSnapshot],
) -> list[dict[str, Any]]:
    """The ``TaskSpec.inputs["materials"]`` payload for a set of snapshots."""
    return [snapshot.to_input_record() for snapshot in snapshots]


__all__ = [
    "MAX_DISPLAY_NAME_CHARS",
    "MaterialSnapshot",
    "material_input_records",
    "safe_attachment_id",
    "snapshot_csv_material",
    "snapshot_json_material",
    "snapshot_text_material",
    "store_material_snapshots",
    "validate_display_name",
]
