"""Shared application services — the layer both the CLI and the TUI call.

Design §11: commands and TUI share application services so every flow is
repeatable without a UI. Nothing in this package imports ``typer`` or
``textual``; the surfaces here are plain Python objects over the trusted
controller and storage ports, and they are the *only* way the CLI/TUI mutate
project state (disconnecting a client never cancels work — state transitions
happen here and in the controller, not in a view).

Services:

* :class:`vouch_agent.appservices.workspace.ProjectWorkspace` — create/open a
  ``.vouch/`` project directory (meta/journal/budget SQLite + content-addressed
  artifacts); fail closed on schema/version mismatches.
* :class:`vouch_agent.appservices.flow.ImprovementFlow` — the improvement
  lifecycle (baseline+rubric freeze, propose/seal, paired evaluation, final
  acceptance, decision/approval/release, explicit reconciliation) wrapping
  :class:`vouch_agent.controller.VouchController`, with the acting role passed
  in and surfaced honestly on refusals.
* :class:`vouch_agent.appservices.reporting.ReportService` — full-burden cost
  report, review overview (candidates/decisions/findings/pending), workflow
  coverage, run/decision histories, evidence/rollback export.
* :mod:`vouch_agent.appservices.adapters` — the built-in deterministic offline
  adapter and the fixture-scenario client that drives the real subprocess
  fixture adapter over protocol v1.
* :mod:`vouch_agent.appservices.packs` — synthetic task-pack construction from
  the in-repo Choose fixture pack.
"""

from vouch_agent.appservices.adapters import (
    FIXTURE_USD_PER_TOKEN,
    FixtureScenarioClient,
    ScriptedOfflineAdapter,
)
from vouch_agent.appservices.flow import (
    BaselineFreeze,
    DecisionOutcome,
    EvaluationOutcome,
    ImprovementFlow,
    ProposalResult,
)
from vouch_agent.appservices.packs import import_fixture_pack
from vouch_agent.appservices.reporting import (
    CostReport,
    ReportService,
    ResumeState,
    ReviewReport,
    RunInfo,
)
from vouch_agent.appservices.workspace import (
    WORKSPACE_DIRNAME,
    WORKSPACE_VERSION,
    ProjectWorkspace,
)

__all__ = [
    "FIXTURE_USD_PER_TOKEN",
    "WORKSPACE_DIRNAME",
    "WORKSPACE_VERSION",
    "BaselineFreeze",
    "CostReport",
    "DecisionOutcome",
    "EvaluationOutcome",
    "FixtureScenarioClient",
    "ImprovementFlow",
    "ProjectWorkspace",
    "ProposalResult",
    "ReportService",
    "ResumeState",
    "ReviewReport",
    "RunInfo",
    "ScriptedOfflineAdapter",
    "import_fixture_pack",
]
