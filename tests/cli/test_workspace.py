"""ProjectWorkspace fail-closed behavior: create/open/version validation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from helpers import init_project

from vouch_agent.appservices.workspace import (
    WORKSPACE_DIRNAME,
    WORKSPACE_FILE,
    ProjectWorkspace,
)
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.errors import ContractError


def _spec(project_id: str = "proj-t", cap: float = 3.0) -> ProjectSpec:
    return ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": project_id,
            "name": "workspace test",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "W-C3",
                    "name": "Compare",
                    "mainObjective": "recordedClaims",
                    "guardrails": ["fabricated-citation"],
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": cap},
        }
    )


def test_create_persists_spec_and_layout(tmp_path: Path) -> None:
    directory = tmp_path / "fresh"
    workspace = ProjectWorkspace.create(directory, _spec())
    vouch = directory / WORKSPACE_DIRNAME
    assert (vouch / WORKSPACE_FILE).is_file()
    assert (vouch / "meta.sqlite").is_file()
    assert (vouch / "journal.sqlite").is_file()
    assert (vouch / "budget.sqlite").is_file()
    assert (vouch / "artifacts").is_dir()
    marker = json.loads((vouch / WORKSPACE_FILE).read_text())
    assert marker["schemaVersion"] == "1"
    assert marker["projectId"] == "proj-t"
    original_spec = workspace.spec
    workspace.close()

    reopened = ProjectWorkspace.open(directory)
    assert reopened.spec == original_spec
    assert reopened.spec.workflows[0].guardrails == ("fabricated-citation",)
    reopened.close()


def test_create_refuses_existing_workspace(tmp_path: Path) -> None:
    directory = tmp_path / "twice"
    first = ProjectWorkspace.create(directory, _spec())
    first.close()
    with pytest.raises(ContractError, match="already exists"):
        ProjectWorkspace.create(directory, _spec())


def test_open_refuses_non_project_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ContractError, match="not a vouch project"):
        ProjectWorkspace.open(empty)


def test_open_refuses_unknown_workspace_version(tmp_path: Path) -> None:
    directory = tmp_path / "ver"
    workspace = ProjectWorkspace.create(directory, _spec())
    workspace.close()
    marker_path = directory / WORKSPACE_DIRNAME / WORKSPACE_FILE
    marker = json.loads(marker_path.read_text())
    marker["schemaVersion"] = "99"
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(ContractError, match="schema version mismatch"):
        ProjectWorkspace.open(directory)


def test_open_refuses_cap_mismatch_between_spec_and_ledger(tmp_path: Path) -> None:
    directory = tmp_path / "cap"
    workspace = ProjectWorkspace.create(directory, _spec(cap=3.0))
    workspace.close()
    # Simulate a spec edited underneath the persisted ledger.
    conn = sqlite3.connect(directory / WORKSPACE_DIRNAME / "meta.sqlite")
    data = json.loads(conn.execute("SELECT data FROM records WHERE kind='project'").fetchone()[0])
    data["budget"]["totalUsdCap"] = 99.0
    conn.execute("UPDATE records SET data=? WHERE kind='project'", (json.dumps(data),))
    conn.commit()
    conn.close()
    with pytest.raises(ContractError, match="cap"):
        ProjectWorkspace.open(directory)


def test_open_refuses_workspace_without_project_record(tmp_path: Path) -> None:
    directory = tmp_path / "noproject"
    workspace = ProjectWorkspace.create(directory, _spec())
    workspace.close()
    conn = sqlite3.connect(directory / WORKSPACE_DIRNAME / "meta.sqlite")
    conn.execute("DELETE FROM records WHERE kind='project'")
    conn.commit()
    conn.close()
    with pytest.raises(ContractError, match="no project record"):
        ProjectWorkspace.open(directory)


def test_cli_init_then_status(tmp_path: Path) -> None:
    from helpers import invoke, out

    project = init_project(tmp_path, cap=2.5)
    result = invoke("status", "--project", str(project))
    assert result.exit_code == 0, out(result)
    assert "proj-proj" in result.stdout
    assert "cap $2.50" in result.stdout
    assert "recovery: clean" in result.stdout
