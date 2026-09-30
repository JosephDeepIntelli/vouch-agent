"""Material snapshot helpers (M4 A3): one digest per material, safe internal
attachment ids separated from user-visible filenames."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_ACCEPTANCE = _TESTS.parent / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

from support import FakeArtifacts  # noqa: E402

from vouch_agent.appservices.materials import (  # noqa: E402
    material_input_records,
    safe_attachment_id,
    snapshot_csv_material,
    snapshot_json_material,
    snapshot_text_material,
    store_material_snapshots,
    validate_display_name,
)
from vouch_agent.contracts.common import digest_bytes  # noqa: E402
from vouch_agent.errors import ContractError  # noqa: E402


class TestOneDigestPerMaterial:
    def test_each_input_snapshot_stored_under_its_own_digest(self) -> None:
        artifacts = FakeArtifacts()
        left = snapshot_csv_material(
            b"sku,price\nA-1,10\n", display_name="catalog.csv", ordinal=0, delimiter=","
        )
        right = snapshot_csv_material(
            b"sku,price\nA-1,12\n", display_name="feed.csv", ordinal=1, delimiter=","
        )
        digests = store_material_snapshots(artifacts, (left, right))
        assert len(digests) == 2
        assert digests[0] == digest_bytes(b"sku,price\nA-1,10\n")
        assert digests[1] == digest_bytes(b"sku,price\nA-1,12\n")
        # each material verifies against its OWN name — no shared blob
        assert artifacts.get(digests[0]) == b"sku,price\nA-1,10\n"
        assert artifacts.get(digests[1]) == b"sku,price\nA-1,12\n"

    def test_two_materials_are_never_one_concatenated_reference(self) -> None:
        """The old shape stored left+b'\\x00'+right under one digest: that
        single digest could not be a reference to either material."""
        artifacts = FakeArtifacts()
        left_bytes, right_bytes = b"left-payload", b"right-payload"
        left = snapshot_text_material(left_bytes, display_name="a.txt", ordinal=0)
        right = snapshot_text_material(right_bytes, display_name="b.txt", ordinal=1)
        digests = store_material_snapshots(artifacts, (left, right))
        concatenated = digest_bytes(left_bytes + b"\x00" + right_bytes)
        assert concatenated not in digests
        assert concatenated not in artifacts.blobs
        # the old shape's failure mode: the concatenated digest is not a
        # readable reference to either material's bytes
        import pytest

        with pytest.raises(ContractError):
            artifacts.get(concatenated)


class TestDisplayNamesAndSafeIds:
    def test_spaces_and_chinese_filenames_work_unchanged(self) -> None:
        snapshot = snapshot_csv_material(
            "sku,名称\nA-1,电水壶\n".encode(),
            display_name="产品 目录.csv",
            ordinal=0,
            delimiter=",",
        )
        assert snapshot.display_name == "产品 目录.csv"
        # the internal id stays contract-safe without mangling the filename
        assert snapshot.attachment.name == safe_attachment_id(0, snapshot.digest)
        allowed = "._-"
        assert all(
            character.isalnum() or character in allowed
            for character in snapshot.attachment.name
        )

    def test_identical_bytes_keep_distinct_display_names(self) -> None:
        payload = b"sku,price\nA-1,10\n"
        one = snapshot_csv_material(payload, display_name="old.csv", ordinal=0, delimiter=",")
        two = snapshot_csv_material(payload, display_name="new.csv", ordinal=1, delimiter=",")
        # same digest, distinct internal ids (ordinal) and distinct names
        assert one.digest == two.digest
        assert one.attachment.name != two.attachment.name
        assert one.display_name != two.display_name

    def test_display_name_validation(self) -> None:
        assert validate_display_name("plain.csv") == "plain.csv"
        assert validate_display_name("我的 数据 2026.json") == "我的 数据 2026.json"
        for bad in ("", "   ", "a/b.csv", "a\\b.csv", "a\x00b", "x" * 201, "line\nbreak"):
            try:
                validate_display_name(bad)
                raise AssertionError(f"{bad!r} must be rejected")
            except ContractError:
                pass

    def test_attachment_id_shape_and_collision_safety(self) -> None:
        digest = digest_bytes(b"x")
        identifier = safe_attachment_id(3, digest)
        assert identifier.startswith("mat-03-")
        assert len(identifier) <= 64
        # distinct ordinals and digests give distinct ids
        assert safe_attachment_id(4, digest) != identifier
        assert safe_attachment_id(3, digest_bytes(b"y")) != identifier
        import re

        from vouch_agent.contracts.materials import TaskAttachment  # noqa: F401 shape check

        assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", identifier)

    def test_duplicate_attachment_ids_refused_at_store_time(self) -> None:
        """Identical bytes snapshotted twice at the SAME ordinal would share
        one internal id — the store call refuses instead of collapsing them."""
        artifacts = FakeArtifacts()
        one = snapshot_text_material(b"same", display_name="one.txt", ordinal=0)
        two = snapshot_text_material(b"same", display_name="two.txt", ordinal=0)
        assert one.attachment.name == two.attachment.name
        try:
            store_material_snapshots(artifacts, (one, two))
            raise AssertionError("colliding attachment ids must be refused")
        except ContractError as exc:
            assert "collided" in str(exc)


class TestSnapshotRecords:
    def test_input_record_carries_id_display_name_and_attachment(self) -> None:
        snapshot = snapshot_json_material(
            b'{"k": 1}', display_name="配置.json", ordinal=2
        )
        record = snapshot.to_input_record()
        assert record["attachmentId"] == snapshot.attachment.name
        assert record["displayName"] == "配置.json"
        assert record["attachment"]["contentDigest"] == snapshot.digest
        assert record["attachment"]["kind"] == "json"

    def test_material_input_records_preserve_order(self) -> None:
        left = snapshot_csv_material(b"a", display_name="a.csv", ordinal=0, delimiter=",")
        right = snapshot_csv_material(b"b", display_name="b.csv", ordinal=1, delimiter=",")
        records = material_input_records((left, right))
        assert [record["displayName"] for record in records] == ["a.csv", "b.csv"]

    def test_oversized_payload_refused(self) -> None:
        from vouch_agent.appservices.materials import MAX_DISPLAY_NAME_CHARS  # noqa: F401
        from vouch_agent.contracts.materials import MAX_MATERIAL_BYTES

        try:
            snapshot_text_material(
                b"x" * (MAX_MATERIAL_BYTES + 1), display_name="big.txt", ordinal=0
            )
            raise AssertionError("oversized material must be refused")
        except ContractError as exc:
            assert "byte cap" in str(exc)

    def test_csv_snapshot_states_its_delimiter(self) -> None:
        snapshot = snapshot_csv_material(
            b"a;b\n", display_name="semi.csv", ordinal=0, delimiter=";"
        )
        assert snapshot.attachment.csv_config["delimiter"] == ";"
        assert snapshot.attachment.kind.value == "csv"
