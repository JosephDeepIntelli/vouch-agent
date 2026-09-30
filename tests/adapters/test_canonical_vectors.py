"""Cross-language canonical-JSON digest vectors (protocol v1.1 §4).

The shared vector file is authored on the Choose runner side
(`scripts/vouch/test-vectors.json`, commit e10c21b in feat/vouch-runner) and
mirrored here verbatim: tests/adapters/data/test-vectors.json. Both language
implementations must produce identical canonical bytes and sha256 digests for
every accepted vector, and reject the same out-of-domain values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch_agent.contracts.common import canonical_json, digest_of
from vouch_agent.errors import ContractError

VECTORS_PATH = Path(__file__).parent / "data" / "test-vectors.json"


@pytest.fixture(scope="module")
def vectors() -> dict:
    data = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == 1
    return data


def test_all_accept_vectors_hash_identically(vectors: dict) -> None:
    accepted = [v for v in vectors["vectors"] if not v.get("expectedRejected")]
    assert len(accepted) >= 20
    for vector in accepted:
        canonical = canonical_json(vector["input"])
        assert canonical == vector["canonical"], vector["name"]
        assert digest_of(vector["input"]) == "sha256:" + vector["sha256"], vector["name"]


def test_rejected_vectors_are_rejected_here_too(vectors: dict) -> None:
    rejected = [v for v in vectors["vectors"] if v.get("expectedRejected")]
    assert len(rejected) == 4
    for vector in rejected:
        with pytest.raises((ContractError, ValueError, OverflowError)):
            canonical_json(vector["input"])


def test_python_domain_guards() -> None:
    # non-finite floats and unsafe integers cannot enter any digest
    with pytest.raises(ContractError, match="non-finite"):
        canonical_json({"cost": float("nan")})
    with pytest.raises(ContractError, match="safe-integer"):
        canonical_json({"n": 2**53})
    with pytest.raises(ContractError, match="safe-integer"):
        canonical_json({"n": 1e16})  # integral float beyond the safe range
    # the boundary itself is fine
    canonical_json({"n": 2**53 - 1})
    canonical_json({"cost": 0.01})
