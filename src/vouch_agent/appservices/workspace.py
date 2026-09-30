"""ProjectWorkspace — a ``.vouch/`` project directory bound to real persistence.

Layout created by :meth:`ProjectWorkspace.create`::

    <project_dir>/
      .vouch/
        workspace.json      # workspace schema version + tool version marker
        meta.sqlite         # contract records (project/pack/rubric/candidate/…)
        journal.sqlite      # append-only events + cost journal
        budget.sqlite       # atomic reservation ledger (cap from ProjectSpec)
        artifacts/          # content-addressed artifact bytes
        adapter-workspace/  # scratch dirs for subprocess adapters (per run)

Open is fail closed (design §13.2): a missing workspace marker, an unknown
workspace schema version, an unreadable/absent project record, or a budget
ledger whose cap disagrees with the ProjectSpec all refuse with a clear error
instead of quietly proceeding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import vouch_agent
from vouch_agent.contracts.common import Role
from vouch_agent.contracts.journal import BudgetReservation, ReservationStatus
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.controller.service import KIND_PROJECT, KIND_RESERVATION, VouchController
from vouch_agent.errors import ContractError
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass
from vouch_agent.storage import (
    TASK_CASE_KIND,
    FileArtifactStore,
    SqliteBudgetLedger,
    SqliteJournal,
    SqliteMetadataStore,
)

WORKSPACE_DIRNAME = ".vouch"
WORKSPACE_VERSION = "1"
WORKSPACE_FILE = "workspace.json"

META_DB = "meta.sqlite"
JOURNAL_DB = "journal.sqlite"
BUDGET_DB = "budget.sqlite"

#: The single adapter action the improvement flow needs the gate to allow.
_GATE_ACTIONS = frozenset({"run-adapter-attempt"})
#: Run ids are ``eval_...``; attempt ids ``att_...`` — both R1 scratch scope.
_GATE_RESOURCE_PREFIXES = ("eval_",)
_GATE_MAX_RESERVATION_USD = 0.5


class ProjectWorkspace:
    """One opened (or freshly created) Vouch project directory.

    The stores are constructed lazily and shared; :meth:`controller` builds a
    fresh controller per call so the gate policy's resource scope always
    reflects the case ids currently imported in the store (a case that was
    never imported cannot be named in an attempt).
    """

    def __init__(self, project_dir: Path, vouch_dir: Path, spec: ProjectSpec | None) -> None:
        self._project_dir = project_dir
        self._vouch_dir = vouch_dir
        self._spec = spec
        self._store: SqliteMetadataStore | None = None
        self._artifacts: FileArtifactStore | None = None
        self._journal: SqliteJournal | None = None
        self._ledger: SqliteBudgetLedger | None = None

    # -- creation / opening ---------------------------------------------------

    @classmethod
    def create(cls, project_dir: Path, spec: ProjectSpec) -> ProjectWorkspace:
        """Create ``<project_dir>/.vouch`` and persist the ProjectSpec.

        Refuses to overwrite an existing workspace (fail closed).
        """
        project_dir = Path(project_dir).resolve()
        vouch_dir = project_dir / WORKSPACE_DIRNAME
        if vouch_dir.exists():
            raise ContractError(
                f"{vouch_dir} already exists; refusing to re-init — open it instead "
                "(or move it away deliberately)"
            )
        project_dir.mkdir(parents=True, exist_ok=True)
        vouch_dir.mkdir(parents=True)
        (vouch_dir / "adapter-workspace").mkdir()
        marker = {
            "schemaVersion": WORKSPACE_VERSION,
            "tool": "vouch-agent",
            "toolVersion": vouch_agent.__version__,
            "projectId": spec.project_id,
        }
        (vouch_dir / WORKSPACE_FILE).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        workspace = cls(project_dir, vouch_dir, spec)
        workspace.controller().init()
        return workspace

    @classmethod
    def open(cls, project_dir: Path) -> ProjectWorkspace:
        """Open an existing workspace, validating every version marker."""
        project_dir = Path(project_dir).resolve()
        vouch_dir = project_dir / WORKSPACE_DIRNAME
        marker_path = vouch_dir / WORKSPACE_FILE
        if not marker_path.is_file():
            raise ContractError(
                f"{project_dir} is not a vouch project (no {WORKSPACE_DIRNAME}/"
                f"{WORKSPACE_FILE}); run 'vouch init' first"
            )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"unreadable workspace marker {marker_path}: {exc}") from exc
        if not isinstance(marker, dict) or marker.get("schemaVersion") != WORKSPACE_VERSION:
            found = marker.get("schemaVersion") if isinstance(marker, dict) else None
            raise ContractError(
                f"workspace schema version mismatch in {marker_path}: found {found!r}, "
                f"this tool supports {WORKSPACE_VERSION!r} — fail closed"
            )
        workspace = cls(project_dir, vouch_dir, spec=None)
        workspace._spec = workspace._load_spec()
        # Opening the ledger re-validates the persisted cap against the spec:
        # a mismatch (e.g. a spec edited underneath the ledger) fails closed.
        _ = workspace.ledger
        return workspace

    def _load_spec(self) -> ProjectSpec:
        ids = self.store.list_ids(KIND_PROJECT)
        if not ids:
            raise ContractError(
                f"workspace at {self._vouch_dir} has no project record; "
                "refusing to open (re-init into a new directory)"
            )
        if len(ids) > 1:
            raise ContractError(
                f"workspace at {self._vouch_dir} holds {len(ids)} project records "
                f"({ids}); a workspace owns exactly one project — fail closed"
            )
        data = self.store.load(KIND_PROJECT, ids[0])
        assert data is not None  # id came from list_ids
        return ProjectSpec.from_dict(data)

    # -- components -------------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    @property
    def vouch_dir(self) -> Path:
        return self._vouch_dir

    @property
    def spec(self) -> ProjectSpec:
        assert self._spec is not None  # set by create()/open()
        return self._spec

    @property
    def store(self) -> SqliteMetadataStore:
        if self._store is None:
            self._store = SqliteMetadataStore(self._vouch_dir / META_DB)
        return self._store

    @property
    def artifacts(self) -> FileArtifactStore:
        if self._artifacts is None:
            self._artifacts = FileArtifactStore(self._vouch_dir)
        return self._artifacts

    @property
    def journal(self) -> SqliteJournal:
        if self._journal is None:
            self._journal = SqliteJournal(self._vouch_dir / JOURNAL_DB)
        return self._journal

    @property
    def ledger(self) -> SqliteBudgetLedger:
        if self._ledger is None:
            self._ledger = SqliteBudgetLedger(
                self._vouch_dir / BUDGET_DB,
                total_usd_cap=self.spec.budget.total_usd_cap,
            )
        return self._ledger

    @property
    def adapter_workspace(self) -> Path:
        return self._vouch_dir / "adapter-workspace"

    def _reservation_status(self, reservation_id: str) -> ReservationStatus:
        data = self.store.load(KIND_RESERVATION, reservation_id)
        if data is None:
            raise ContractError(
                f"reservation {reservation_id!r} is unknown to this workspace; "
                "cannot re-verify the gate binding (fail closed)"
            )
        record = BudgetReservation.from_dict(data)
        return record.status if record.status is not None else ReservationStatus.OPEN

    def _gate_scope(self) -> frozenset[str]:
        """Case ids imported into this workspace may be named by R1 attempts."""
        return frozenset(self.store.list_ids(TASK_CASE_KIND))

    def broker(self) -> CapabilityBroker:
        policy = GatePolicy(
            allowed_actions=_GATE_ACTIONS,
            max_risk_class=RiskClass.R1,
            allowed_resources=self._gate_scope(),
            resource_prefixes=_GATE_RESOURCE_PREFIXES,
            max_reservation_usd=_GATE_MAX_RESERVATION_USD,
        )
        return CapabilityBroker(policy, self.ledger, self.journal, self._reservation_status)

    def controller(self) -> VouchController:
        """A controller wired to this workspace's persistence and gate."""
        return VouchController(
            self.spec, self.store, self.artifacts, self.ledger, self.journal, self.broker()
        )

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> ProjectWorkspace:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def workspace_info(self) -> dict[str, Any]:
        marker = json.loads((self._vouch_dir / WORKSPACE_FILE).read_text(encoding="utf-8"))
        return dict(marker)

    def describe_role(self, role: Role) -> str:
        """The human identity holding ``role`` in this project, if declared."""
        return self.spec.owners.get(role, "")
