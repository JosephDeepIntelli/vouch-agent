"""Task materials (M3 Workstream B4) — immutable, typed, content-addressed.

Materials are the authorized inputs a task works on: JSON objects, UTF-8
text, or CSV tabular data. They are snapshotted by digest at submission and
never mutate during a run; a changed input is a NEW material (and therefore
a new result), which is what makes changed-input sensitivity honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import ContractRecord, require_digest, require_str
from vouch_agent.errors import ContractError

#: Media types a native task may attach (kept deliberately small).
SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "text/plain; charset=utf-8",
        "text/csv",
    }
)

#: Per-attachment byte cap — materials are bounded inputs, not data stores.
MAX_MATERIAL_BYTES = 8 * 1024 * 1024


class MaterialKind(StrEnum):
    JSON = "json"
    TEXT = "text"
    CSV = "csv"


@dataclass(frozen=True)
class TaskAttachment(ContractRecord):
    """One immutable material reference. Bytes live in the artifact store;
    this record is the durable, digest-bound reference."""

    name: str
    kind: MaterialKind
    media_type: str
    content_digest: str  # sha256 of the exact bytes (NOT 'digest': that name
    # would inherit ContractRecord.digest() as a dataclass default from the base)
    size_bytes: int
    # CSV attachments must state their join/identity semantics up front so the
    # operation's comparison rules are explicit, not guessed.
    csv_config: dict[str, Any] = field(default_factory=dict)  # delimiter, header, encoding notes
    schema_version: str = "1"

    def __post_init__(self) -> None:
        import re

        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.name):
            raise ContractError(f"material name {self.name!r} must be 1-64 chars of [A-Za-z0-9._-]")
        if self.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ContractError(
                f"unsupported material media type {self.media_type!r}; "
                f"supported: {sorted(SUPPORTED_MEDIA_TYPES)}"
            )
        if self.size_bytes < 0 or self.size_bytes > MAX_MATERIAL_BYTES:
            raise ContractError(
                f"material {self.name!r} size {self.size_bytes} outside 0..{MAX_MATERIAL_BYTES}"
            )
        if self.kind is MaterialKind.CSV and "delimiter" not in self.csv_config:
            raise ContractError(
                f"CSV material {self.name!r} must state its delimiter in csv_config"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "kind": self.kind.value,
            "mediaType": self.media_type,
            "contentDigest": self.content_digest,
            "sizeBytes": self.size_bytes,
            "csvConfig": self.csv_config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskAttachment:
        cls._check_version(data)
        return cls(
            name=require_str(data["name"], "name"),
            kind=MaterialKind(data["kind"]),
            media_type=require_str(data["mediaType"], "mediaType"),
            content_digest=require_digest(data["contentDigest"], "contentDigest"),
            size_bytes=int(data["sizeBytes"]),
            csv_config=dict(data.get("csvConfig") or {}),
        )


def classify_media_type(payload: bytes, name: str) -> MaterialKind:
    """Best-effort classification by content, cross-checked for sanity.

    JSON must parse; CSV must contain a delimiter-bearing header line; text
    must decode as UTF-8. The caller stores the verified bytes.
    """
    import json as _json

    if len(payload) > MAX_MATERIAL_BYTES:
        raise ContractError(f"material {name!r} exceeds the {MAX_MATERIAL_BYTES}-byte cap")
    stripped = payload.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            _json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ContractError(
                f"material {name!r} looks like JSON but does not parse: {exc}"
            ) from exc
        return MaterialKind.JSON
    first_line = payload.split(b"\n", 1)[0]
    looks_tabular = b"," in first_line or b";" in first_line or b"\t" in first_line
    if looks_tabular and name.lower().endswith(".csv"):
        return MaterialKind.CSV
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"material {name!r} is not valid UTF-8: {exc}") from exc
    return MaterialKind.TEXT
