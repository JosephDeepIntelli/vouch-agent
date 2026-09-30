"""Fixtures for the CLI tests; plain helpers live in helpers.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import baseline, fixture_pack, init_project, propose_sealed


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """An initialized project with baseline+rubric, a synthetic pack and a
    sealed candidate, ready for `vouch evaluate`."""
    directory = init_project(tmp_path)
    assert baseline(directory).exit_code == 0
    assert fixture_pack(directory).exit_code == 0
    assert propose_sealed(directory).exit_code == 0
    return directory
