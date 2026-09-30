"""Materials contract: typed, bounded, immutable references (lead-owned)."""

from __future__ import annotations

import json

import pytest

from vouch_agent.contracts.common import digest_bytes
from vouch_agent.contracts.materials import (
    MAX_MATERIAL_BYTES,
    MaterialKind,
    TaskAttachment,
    classify_media_type,
)
from vouch_agent.errors import ContractError


def _attachment(**overrides) -> TaskAttachment:
    fields = {
        "name": "catalog.csv",
        "kind": MaterialKind.CSV,
        "media_type": "text/csv",
        "content_digest": digest_bytes(b"a,b\n1,2\n"),
        "size_bytes": 8,
        "csv_config": {"delimiter": ",", "header": True},
    }
    fields.update(overrides)
    return TaskAttachment(**fields)


def test_attachment_roundtrip_and_defaults() -> None:
    att = _attachment()
    restored = TaskAttachment.from_dict(json.loads(att.to_canonical_json()))
    assert restored.content_digest == att.content_digest
    assert restored.csv_config["delimiter"] == ","


def test_bad_names_types_sizes_rejected() -> None:
    with pytest.raises(ContractError, match="name"):
        _attachment(name="../escape")
    with pytest.raises(ContractError, match="media type"):
        _attachment(media_type="application/octet-stream")
    with pytest.raises(ContractError, match="size"):
        _attachment(size_bytes=MAX_MATERIAL_BYTES + 1)


def test_csv_requires_delimiter_config() -> None:
    with pytest.raises(ContractError, match="delimiter"):
        _attachment(csv_config={})


def test_classification_json_csv_text() -> None:
    assert classify_media_type(b'{"a": 1}', "m") is MaterialKind.JSON
    assert classify_media_type(b"a,b\n1,2\n", "rows.csv") is MaterialKind.CSV
    assert classify_media_type("迪普智选 plain".encode(), "m") is MaterialKind.TEXT
    with pytest.raises(ContractError, match="JSON but does not parse"):
        classify_media_type(b"{not json", "m")
    with pytest.raises(ContractError, match="UTF-8"):
        classify_media_type(b"\xff\xfe\x00bad", "m")
    with pytest.raises(ContractError, match="byte cap"):
        classify_media_type(b"x" * (MAX_MATERIAL_BYTES + 1), "m")
