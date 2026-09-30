"""Shared helpers for the M3 runtime regression tests.

Creates a real native ``.vouch`` workspace (SQLite stores + artifact tree)
without driving the CLI, so the regressions exercise the ExecutionService
default path — the same service layer ``vouch run`` and the TUI call —
directly and quickly.
"""

from __future__ import annotations

from pathlib import Path

from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.project import ProjectSpec

_NATIVE_SPEC: dict = {
    "schemaVersion": "1",
    "projectId": "proj-m3-runtime-regression",
    "name": "m3-runtime-regression",
    "workflows": [
        {
            "schemaVersion": "1",
            "workflowId": "W-C3",
            "name": "Compare",
            "mainObjective": "recordedClaims",
        }
    ],
    "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
    "allowedChangeTypes": ["prompt-delta"],
    "budget": {"schemaVersion": "1", "totalUsdCap": 5.0},
}


def init_native_project(tmp_path: Path) -> Path:
    """A fresh native workspace directory (synthetic spec, no CLI)."""
    project = tmp_path / "proj"
    spec = ProjectSpec.from_dict(dict(_NATIVE_SPEC))
    ProjectWorkspace.create(project, spec).close()
    return project
