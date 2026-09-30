"""Vouch TUI — a modest daily-review surface over the shared app services.

Design §11/§10: the TUI is a *disconnectable client*. It never mutates state
directly: every read goes through :class:`vouch_agent.appservices.reporting.ReportService`
and every action (open/init/export) through the app services, so closing or
killing the TUI can never cancel work — all state transitions happen in the
services/controller, persisted in the workspace.

Screens: Project (open/init), Task (direct bounded execution through
ExecutionService), Review (candidates + decisions + costs + findings),
Run (evaluation progress from journal events), Export (evidence/rollback
packages with the manifest digest).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vouch_agent.appservices.workspace import ProjectWorkspace


class VouchApp(App[None]):
    TITLE = "Vouch — improvement evidence review"
    CSS = """
    Screen { layers: initial; }
    #notice { color: $warning; }
    #error { color: $error; }
    """
    BINDING_TITLE = "Vouch"

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+p", "app.switch_screen('project')", "Project", show=True),
        Binding("ctrl+t", "app.switch_screen('task')", "Task", show=True),
        Binding("ctrl+r", "app.switch_screen('review')", "Review", show=True),
        Binding("ctrl+u", "app.switch_screen('run')", "Run", show=True),
        Binding("ctrl+e", "app.switch_screen('export')", "Export", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
    ]

    def __init__(self, project_dir: Path | str | None = None) -> None:
        super().__init__()
        self._project_dir: Path | None = Path(project_dir) if project_dir else None
        self.workspace: ProjectWorkspace | None = None

    # -- lifecycle ---------------------------------------------------------------

    def on_mount(self) -> None:
        from vouch_agent.tui.screens import (
            ExportScreen,
            ProjectScreen,
            ReviewScreen,
            RunScreen,
            TaskScreen,
        )

        for name, screen in (
            ("project", ProjectScreen()),
            ("task", TaskScreen()),
            ("review", ReviewScreen()),
            ("run", RunScreen()),
            ("export", ExportScreen()),
        ):
            self.install_screen(screen, name=name)
        if self._project_dir is not None:
            self.open_project(str(self._project_dir), switch=False)
        self.push_screen("project")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()

    # -- workspace management ------------------------------------------------------

    def open_project(self, path_text: str, *, switch: bool = True) -> bool:
        """Open a workspace through ProjectWorkspace (fail closed, no crash)."""
        from vouch_agent.appservices.workspace import ProjectWorkspace
        from vouch_agent.errors import VouchError

        path = Path(path_text.strip()).expanduser()
        try:
            workspace = ProjectWorkspace.open(path)
        except VouchError as exc:
            self._set_status(f"open failed [{exc.code}]: {exc}", error=True)
            return False
        if self.workspace is not None:
            self.workspace.close()
        self.workspace = workspace
        self._project_dir = path
        self._set_status(
            f"opened {workspace.spec.project_id} ({workspace.spec.name}) — "
            f"{len(workspace.spec.workflows)} workflow(s)"
        )
        if switch:
            self.switch_screen("review")
        return True

    def init_project(
        self,
        path_text: str,
        workflow_id: str,
        acceptance_owner: str,
        release_owner: str,
        cap: float,
    ) -> bool:
        """Create a new project workspace through the service layer."""
        from vouch_agent.contracts.project import ProjectSpec
        from vouch_agent.errors import VouchError
        from vouch_agent.workflows import DECLARED_CHOOSE_WORKFLOWS

        declared = {spec.workflow_id: spec for spec in DECLARED_CHOOSE_WORKFLOWS}
        if workflow_id not in declared:
            self._set_status(
                f"unknown workflow {workflow_id!r}; declared: {', '.join(sorted(declared))}",
                error=True,
            )
            return False
        spec = ProjectSpec.from_dict(
            {
                "schemaVersion": "1",
                "projectId": f"proj-{Path(path_text.strip()).name.lower()}",
                "name": Path(path_text.strip()).name,
                "workflows": [
                    {
                        "schemaVersion": "1",
                        "workflowId": workflow_id,
                        "name": declared[workflow_id].name,
                        "mainObjective": "recordedClaims",
                        "guardrails": [],
                    }
                ],
                "owners": {
                    "acceptance-owner": acceptance_owner,
                    "release-owner": release_owner,
                },
                "allowedChangeTypes": ["prompt-delta"],
                "budget": {"schemaVersion": "1", "totalUsdCap": cap},
            }
        )
        try:
            from vouch_agent.appservices.workspace import ProjectWorkspace

            workspace = ProjectWorkspace.create(Path(path_text.strip()).expanduser(), spec)
        except VouchError as exc:
            self._set_status(f"init failed [{exc.code}]: {exc}", error=True)
            return False
        self.workspace = workspace
        self._project_dir = Path(path_text.strip()).expanduser()
        self._set_status(f"initialized {self._project_dir / '.vouch'}")
        self.switch_screen("review")
        return True

    def require_workspace(self) -> ProjectWorkspace | None:
        if self.workspace is None:
            self._set_status("no project open — open or init one (ctrl+p)", error=True)
        return self.workspace

    # -- helpers ----------------------------------------------------------------------

    def _set_status(self, message: str, *, error: bool = False) -> None:
        from vouch_agent.tui.screens import status_widget

        widget = status_widget(self)
        if widget is not None:
            widget.update(message)
            if error:
                widget.add_class("error")
            else:
                widget.remove_class("error")


def run_tui(project_dir: str | None = None) -> None:
    """Entry point for ``vouch tui``."""
    VouchApp(project_dir).run()
