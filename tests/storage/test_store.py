"""Metadata store + artifact store behavior (incl. path-escape attacks)."""

from __future__ import annotations

import json

import pytest

from vouch_agent.contracts.common import digest_bytes
from vouch_agent.errors import ContractError, DigestMismatchError
from vouch_agent.storage.store import FileArtifactStore, SqliteMetadataStore


def test_save_load_roundtrip(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    record = {"schemaVersion": "1", "hello": "世界", "n": 3}
    store.save("task-pack", "pack_1", record)
    assert store.load("task-pack", "pack_1") == record
    assert store.load("task-pack", "missing") is None
    assert store.list_ids("task-pack") == ["pack_1"]


def test_save_upserts_same_kind_and_id(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    store.save("candidate", "c1", {"v": 1})
    store.save("candidate", "c1", {"v": 2})
    assert store.load("candidate", "c1") == {"v": 2}
    assert store.list_ids("candidate") == ["c1"]


def test_kinds_are_isolated_namespaces(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    store.save("a", "x", {"who": "a"})
    store.save("b", "x", {"who": "b"})
    assert store.load("a", "x") == {"who": "a"}
    assert store.load("b", "x") == {"who": "b"}


def test_save_rejects_empty_identifiers_and_non_dicts(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    with pytest.raises(ContractError):
        store.save("", "x", {})
    with pytest.raises(ContractError):
        store.save("a", "", {})
    with pytest.raises(ContractError):
        store.save("a", "x", ["not", "a", "dict"])  # type: ignore[arg-type]


def test_transaction_commits_atomically(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    with store.transaction():
        store.save("evaluation-run", "r1", {"n": 1})
        store.save("evaluation-run", "r2", {"n": 2})
    assert store.list_ids("evaluation-run") == ["r1", "r2"]


def test_transaction_rolls_back_on_exception(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.save("evaluation-run", "r1", {"n": 1})
            store.save("evaluation-run", "r2", {"n": 2})
            raise RuntimeError("boom")
    assert store.list_ids("evaluation-run") == []
    # The store remains usable after a rollback.
    store.save("evaluation-run", "r3", {"n": 3})
    assert store.load("evaluation-run", "r3") == {"n": 3}


def test_nested_transactions_behave_like_savepoints(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    with store.transaction():
        store.save("candidate", "outer", {"v": 1})
        with pytest.raises(ValueError):
            with store.transaction():
                store.save("candidate", "inner-bad", {"v": 2})
                raise ValueError("inner boom")
        # inner rollback must not take the outer transaction down
        store.save("candidate", "inner-good", {"v": 3})
    assert store.list_ids("candidate") == ["inner-good", "outer"]


def test_store_survives_reopen(tmp_path):
    db = tmp_path / "meta.db"
    store = SqliteMetadataStore(db)
    store.save("rubric", "rub_1", {"thresholds": {"min-main-improvement": 0.05}})
    store.close()
    reopened = SqliteMetadataStore(db)
    assert reopened.load("rubric", "rub_1") == {"thresholds": {"min-main-improvement": 0.05}}


def test_persisted_payload_is_canonical_json(tmp_path):
    store = SqliteMetadataStore(tmp_path / "meta.db")
    store.save("k", "id", {"z": 1, "a": 2})
    import sqlite3

    conn = sqlite3.connect(tmp_path / "meta.db")
    raw = conn.execute("SELECT data FROM records").fetchone()[0]
    conn.close()
    assert raw == json.dumps({"z": 1, "a": 2}, sort_keys=True, separators=(",", ":"))


# -- artifacts ---------------------------------------------------------------


def test_artifact_put_get_exists(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    payload = b"choose fixture input"
    digest = artifacts.put(payload)
    assert digest.startswith("sha256:") and len(digest) == 71
    assert digest == digest_bytes(payload)
    assert artifacts.exists(digest)
    assert artifacts.get(digest) == payload


def test_artifact_put_is_idempotent_and_content_addressed(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    d1 = artifacts.put(b"same bytes")
    d2 = artifacts.put(b"same bytes")
    other = artifacts.put(b"different bytes")
    assert d1 == d2 != other


def test_artifact_lives_under_artifacts_directory(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    digest = artifacts.put(b"payload")
    hex_name = digest.removeprefix("sha256:")
    assert (tmp_path / "artifacts" / hex_name).is_file()


def test_artifact_refuses_path_escape_names(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    for attack in (
        "sha256:../../etc/passwd",
        "sha256:../../../../etc/passwd\x00",
        "../escape",
        "sha256:/abs/path",
        "",
        "md5:abcd",
        "sha256:NOTHEX",
    ):
        with pytest.raises(ContractError):
            artifacts.get(attack)
        with pytest.raises(ContractError):
            artifacts.exists(attack)


def test_artifact_get_detects_tampering(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    digest = artifacts.put(b"original")
    target = tmp_path / "artifacts" / digest.removeprefix("sha256:")
    target.write_bytes(b"tampered")
    with pytest.raises(DigestMismatchError):
        artifacts.get(digest)


def test_artifact_get_missing_raises(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    missing = digest_bytes(b"never stored")
    with pytest.raises(ContractError, match="not found"):
        artifacts.get(missing)


def test_artifact_put_rejects_non_bytes(tmp_path):
    artifacts = FileArtifactStore(tmp_path)
    with pytest.raises(ContractError):
        artifacts.put("a string is not bytes")  # type: ignore[arg-type]
