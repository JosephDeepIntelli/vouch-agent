"""Detached worker entry points + native result export (M3-B1/B2/B5).

Compatibility facade (M4 A1 ownership split): the worker LIFECYCLE — fenced
lease, explicit start/resume commands, cancel polling, the detached entry —
moved to :mod:`vouch_agent.appservices.worker_lifecycle`; the native EXPORT
lives in :mod:`vouch_agent.appservices.native_export`. This module keeps the
historical import surface (``vouch_agent.appservices.worker``) stable for the
CLI and TUI clients.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vouch_agent.appservices.worker_lifecycle import (
    KIND_WORKER_COMMAND,
    KIND_WORKER_LEASE,
    execute_run_detached,
    issue_worker_command,
    spawn_detached_worker,
    worker_lease,
)

__all__ = [
    "KIND_WORKER_COMMAND",
    "KIND_WORKER_LEASE",
    "execute_run_detached",
    "export_run",
    "issue_worker_command",
    "spawn_detached_worker",
    "worker_lease",
]


def export_run(*args: Any, **kwargs: Any) -> Path:
    """Compatibility re-export (moved to vouch_agent.appservices.native_export)."""
    from vouch_agent.appservices.native_export import export_run as _export

    return _export(*args, **kwargs)
