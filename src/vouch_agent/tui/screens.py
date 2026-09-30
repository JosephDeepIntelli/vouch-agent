"""TUI screens — thin views over the application services.

Every screen renders data fetched through
:class:`vouch_agent.appservices.reporting.ReportService` (or, for export, the
same service the CLI uses). No screen writes project state directly, and no
long-running work is owned by the TUI: closing it mid-view leaves the
workspace byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Label, Static

from vouch_agent.tui.app import VouchApp

_MAX_EVENTS = 200


def _app(widget: Screen[None]) -> VouchApp:
    app: App = widget.app
    return app  # type: ignore[return-value]  # the TUI only ever runs on VouchApp


def status_widget(app: App) -> Static | None:
    """The current screen's status line, if it has one."""
    from textual.css.query import NoMatches

    try:
        return app.screen.query_one("#status", Static)
    except NoMatches:  # pragma: no cover - screens without a status line
        return None
    except Exception:  # pragma: no cover - no screen mounted yet
        return None


class ProjectScreen(Screen[None]):
    """Open an existing project or init a new one (all through services)."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("o", "open", "Open")
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Project — open or initialize"),
            Input(placeholder="/path/to/project (contains .vouch/)", id="open-path"),
            Button("Open", id="btn-open"),
            Label("— or init a new synthetic-fixture project —"),
            Input(placeholder="new project directory", id="init-path"),
            Input(placeholder="workflow id (e.g. W-C3)", id="init-workflow"),
            Input(placeholder="acceptance owner", id="init-acceptance"),
            Input(placeholder="release owner", id="init-release"),
            Input(placeholder="budget cap USD (default 5.0)", id="init-cap"),
            Button("Init", id="btn-init"),
            Static("", id="status"),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = _app(self)
        if event.button.id == "btn-open":
            path = self.query_one("#open-path", Input).value
            if not path.strip():
                app._set_status("enter a project directory path first", error=True)
                return
            app.open_project(path)
        elif event.button.id == "btn-init":
            self._init(app)

    def action_open(self) -> None:
        app = _app(self)
        path = self.query_one("#open-path", Input).value
        if path.strip():
            app.open_project(path)

    def _init(self, app: VouchApp) -> None:
        path = self.query_one("#init-path", Input).value.strip()
        workflow = self.query_one("#init-workflow", Input).value.strip()
        acceptance = self.query_one("#init-acceptance", Input).value.strip()
        release = self.query_one("#init-release", Input).value.strip()
        cap_text = self.query_one("#init-cap", Input).value.strip()
        try:
            cap = float(cap_text) if cap_text else 5.0
        except ValueError:
            app._set_status(f"cap must be a number, got {cap_text!r}", error=True)
            return
        if not (path and workflow and acceptance and release):
            app._set_status(
                "init needs path, workflow, acceptance owner and release owner",
                error=True,
            )
            return
        app.init_project(path, workflow, acceptance, release, cap)


class ReviewScreen(Screen[None]):
    """Candidates with states, decisions, findings, costs, recovery posture."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh")
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Review — candidates, decisions, guardrails, costs"),
            DataTable(id="candidates"),
            DataTable(id="decisions"),
            Static("", id="findings"),
            Static("", id="costs"),
            Static("", id="status"),
            Button("Refresh", id="btn-refresh"),
        )

    def on_screen_resume(self) -> None:
        self.action_refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.action_refresh()

    def action_refresh(self) -> None:
        app = _app(self)
        workspace = app.require_workspace()
        if workspace is None:
            return
        from vouch_agent.appservices.reporting import ReportService
        from vouch_agent.errors import VouchError

        try:
            report = ReportService(workspace).review()
        except VouchError as exc:
            app._set_status(f"review failed [{exc.code}]: {exc}", error=True)
            return
        candidates = self.query_one("#candidates", DataTable)
        candidates.clear(columns=True)
        candidates.add_columns("candidate", "state", "change", "proposer", "digest")
        for candidate in report.candidates:
            candidates.add_row(
                candidate.candidate_id,
                candidate.state.value,
                candidate.change_type.value,
                candidate.proposer,
                candidate.content_digest().removeprefix("sha256:")[:12],
            )
        decisions = self.query_one("#decisions", DataTable)
        decisions.clear(columns=True)
        decisions.add_columns("decision", "verdict", "owner", "evidence")
        for decision in report.decisions:
            decisions.add_row(
                decision.decision_id,
                decision.verdict.value,
                decision.owner,
                decision.evidence_digest.removeprefix("sha256:")[:12],
            )
        findings = self.query_one("#findings", Static)
        if report.hard_findings:
            findings.update(
                "HARD guardrail findings:\n"
                + "\n".join(
                    f"  {run_id}: {f.name} — {f.detail}" for run_id, f in report.hard_findings
                )
            )
        else:
            findings.update("hard guardrail findings: none")
        costs = report.costs
        resume = report.resume
        self.query_one("#costs", Static).update(
            f"costs: measured ${costs.measured_usd:.4f} / cap ${costs.total_cap_usd:.2f} "
            f"(reserved ${costs.outstanding_reserved_usd:.4f}, remaining "
            f"${costs.remaining_usd:.4f}, human {costs.human_minutes:.0f}min, "
            f"unmeasurable entries {len(costs.unmeasurable_entries)})\n"
            f"recovery: {len(resume.open_reservations)} open reservation(s), "
            f"{len(resume.needs_reconciliation)} subject(s) need reconciliation\n"
            "coverage: "
            + ", ".join(f"{len(ids)} {status}" for status, ids in sorted(report.coverage.items()))
            + " (fixture-covered proves the pipeline only)"
        )
        pending = ", ".join(f"{c.candidate_id} ({c.state.value})" for c, _ in report.pending)
        app._set_status(f"pending: {pending or 'nothing'} — {len(report.candidates)} candidate(s)")


class RunScreen(Screen[None]):
    """Evaluation progress from the journal (read-only view)."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh")
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Run — journal events (append-only evidence trail)"),
            Input(placeholder="filter by subject (run/attempt id), empty = all", id="subject"),
            DataTable(id="events"),
            Static("", id="status"),
            Button("Refresh", id="btn-refresh"),
        )

    def on_screen_resume(self) -> None:
        self.action_refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.action_refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        app = _app(self)
        workspace = app.require_workspace()
        if workspace is None:
            return
        from vouch_agent.appservices.reporting import ReportService
        from vouch_agent.errors import VouchError

        subject = self.query_one("#subject", Input).value.strip() or None
        try:
            events = ReportService(workspace).events(subject)
        except VouchError as exc:
            app._set_status(f"journal read failed [{exc.code}]: {exc}", error=True)
            return
        table = self.query_one("#events", DataTable)
        table.clear(columns=True)
        table.add_columns("at", "kind", "subject", "detail")
        for event in list(events)[-_MAX_EVENTS:]:
            detail = ", ".join(f"{key}={value}" for key, value in list(event.data.items())[:3])
            table.add_row(event.occurred_at[11:23], event.kind.value, event.subject, detail)
        app._set_status(f"{len(events)} journal event(s){' (filtered)' if subject else ''}")


class ExportScreen(Screen[None]):
    """Export evidence / rollback packages through the report service."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Export — deterministic evidence / rollback packages"),
            Input(placeholder="evaluation run id (evidence)", id="run-id"),
            Input(placeholder="candidate id (rollback)", id="candidate-id"),
            Input(placeholder="destination directory (empty/new)", id="destination"),
            Button("Export evidence", id="btn-evidence"),
            Button("Export rollback plan", id="btn-rollback"),
            Static("", id="result"),
            Static("", id="status"),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = _app(self)
        workspace = app.require_workspace()
        if workspace is None:
            return
        from vouch_agent.appservices.reporting import ReportService
        from vouch_agent.errors import VouchError

        destination_text = self.query_one("#destination", Input).value.strip()
        result = self.query_one("#result", Static)
        if not destination_text:
            app._set_status("destination directory required", error=True)
            return
        destination = Path(destination_text).expanduser()
        service = ReportService(workspace)
        try:
            if event.button.id == "btn-evidence":
                run_id = self.query_one("#run-id", Input).value.strip()
                if not run_id:
                    app._set_status("run id required for evidence export", error=True)
                    return
                package = service.export_run(run_id, destination=destination)
            elif event.button.id == "btn-rollback":
                candidate_id = self.query_one("#candidate-id", Input).value.strip()
                if not candidate_id:
                    app._set_status("candidate id required for rollback export", error=True)
                    return
                package = service.export_rollback(candidate_id, destination=destination)
            else:  # pragma: no cover - unknown button
                return
        except VouchError as exc:
            result.update("")
            app._set_status(f"export failed [{exc.code}]: {exc}", error=True)
            return
        result.update(
            f"package: {package.package_kind} -> {package.path}\n"
            f"files: {len(package.file_digests)}  mode: {package.mode.value}\n"
            f"manifest digest: {package.manifest_digest}"
        )
        app._set_status("export complete — verify with 'vouch export' or verify_package()")


class TaskScreen(Screen[None]):
    """Direct bounded task execution — a RESPONSIVE client (M4 §B).

    Start routes through the detached worker (independent process); the
    handler never blocks the UI thread on execution. Progress polls durable
    state on a timer; pause/resume/cancel are explicit client commands over
    the same services. Closing the TUI mid-run leaves the worker running.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh runs"),
        ("p", "pause_selected", "Pause selected"),
        ("e", "resume_selected", "rEsume selected"),
        ("x", "cancel_selected", "Cancel selected"),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Task — execute a bounded task (deterministic operations; fixture providers)"),
            Input(placeholder="goal: what must the task accomplish", id="task-goal"),
            Input(placeholder="input name=path.json (task material)", id="task-input"),
            Input(
                placeholder="required fields, comma separated (completion check)", id="task-require"
            ),
            Input(placeholder="budget USD (synthetic)", id="task-budget"),
            Horizontal(
                Button("Start (background worker)", id="btn-task-run"),
                Button("Refresh", id="btn-task-refresh"),
                Button("Pause", id="btn-task-pause"),
                Button("Resume", id="btn-task-resume"),
                Button("Cancel", id="btn-task-cancel"),
            ),
            Static("", id="task-result"),
            DataTable(id="task-runs", cursor_type="row"),
            Static("", id="task-detail"),
        )

    def on_screen_resume(self) -> None:
        self.action_refresh()
        self.set_interval(2.0, self.action_refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-task-run":
            self._start()
        elif event.button.id == "btn-task-refresh":
            self.action_refresh()
        elif event.button.id == "btn-task-pause":
            self.action_pause_selected()
        elif event.button.id == "btn-task-resume":
            self.action_resume_selected()
        elif event.button.id == "btn-task-cancel":
            self.action_cancel_selected()

    # --- start (non-blocking: the worker is an independent process) ------------

    def _start(self) -> None:
        app = self.app
        if not isinstance(app, VouchApp) or app.workspace is None:
            self._result("open a project first (ctrl+p)")
            return
        import json as _json

        goal = self.query_one("#task-goal", Input).value.strip()
        input_token = self.query_one("#task-input", Input).value.strip()
        require_raw = self.query_one("#task-require", Input).value.strip()
        budget_raw = self.query_one("#task-budget", Input).value.strip()
        if not goal or "=" not in input_token:
            self._result("provide a goal and an input like fact=path.json")
            return
        name, _, path = input_token.partition("=")
        try:
            materials = {
                name.strip(): _json.loads(Path(path.strip()).read_text(encoding="utf-8"))
            }
        except (OSError, ValueError) as exc:
            self._result(f"cannot read input: {exc}")
            return
        conditions: list[dict[str, object]] = []
        required = [f.strip() for f in require_raw.split(",") if f.strip()]
        if required:
            conditions.append(
                {
                    "type": "artifact_schema",
                    "schema": {
                        "type": "object",
                        "required": required,
                        "properties": {f: {"type": "string"} for f in required},
                    },
                }
            )
        try:
            budget = float(budget_raw) if budget_raw else 0.5
        except ValueError:
            self._result("budget must be a number")
            return
        from vouch_agent.appservices.execution import ExecutionService
        from vouch_agent.appservices.worker import spawn_detached_worker

        service = ExecutionService(app.workspace)
        run_id = service.submit(
            goal=goal,
            inputs=materials,
            budget_usd=budget,
            max_steps=4,
            completion_conditions=conditions,
        )
        pid = spawn_detached_worker(app.workspace.project_dir, run_id)
        self._result(
            f"started {run_id} in background worker (pid {pid}) — you may close this "
            "client; work continues. Select the row and use Pause/Resume/Cancel."
        )
        self.action_refresh()

    # --- lifecycle commands on the selected run ----------------------------------

    def _selected_run(self) -> str | None:
        table = self.query_one("#task-runs", DataTable)
        try:
            row = table.get_row_at(table.cursor_row)
        except Exception:
            return None
        return str(row[0]) if row else None

    def action_pause_selected(self) -> None:
        run_id = self._selected_run()
        if run_id:
            self._command(run_id, "pause")

    def action_resume_selected(self) -> None:
        run_id = self._selected_run()
        if run_id:
            self._command(run_id, "resume")

    def action_cancel_selected(self) -> None:
        run_id = self._selected_run()
        if run_id:
            self._command(run_id, "cancel")

    def _command(self, run_id: str, kind: str) -> None:
        app = self.app
        if not isinstance(app, VouchApp) or app.workspace is None:
            return
        from vouch_agent.appservices.execution import ExecutionService

        service = ExecutionService(app.workspace)
        if kind == "pause":
            app.workspace.store.save("pause-request", run_id, {"requested": True})
            self._result(f"pause requested for {run_id}")
        elif kind == "cancel":
            try:
                service.cancel(run_id, "cancelled from TUI")
                self._result(f"cancel requested for {run_id}")
            except Exception as exc:
                self._result(f"cancel refused for {run_id}: {exc}")
        elif kind == "resume":
            # Route continuation through a fresh detached worker carrying an
            # EXPLICIT durable resume command (never this client process, and
            # never a default `start` spawn: the worker that paused this run
            # already acknowledged `start`, so a start spawn is refused by the
            # command gate and the run would silently stay paused).
            from vouch_agent.appservices.worker import spawn_detached_worker
            from vouch_agent.appservices.worker_lifecycle import (
                issue_worker_command,
                worker_lease,
            )
            from vouch_agent.contracts.tasks import TaskStatus

            run = service.status(run_id)
            lease = worker_lease(app.workspace, run_id)
            if run is None:
                self._result(f"resume refused for {run_id}: unknown run")
            elif run.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                self._result(
                    f"resume refused for {run_id}: the run is already "
                    f"{run.status.value}"
                )
            elif lease is not None and lease.get("alive"):
                # One authoritative execution: a live worker owns the run, a
                # second dispatch would be refused at its lease anyway.
                self._result(
                    f"resume refused for {run_id}: a worker is already executing "
                    "this run"
                )
            else:
                command = issue_worker_command(app.workspace, run_id, "resume")
                pid = spawn_detached_worker(
                    app.workspace.project_dir,
                    run_id,
                    provider="extract-fact",
                    command="resume",
                    command_version=int(command["version"]),
                )
                self._result(
                    f"resume: {run_id} dispatched to worker pid {pid} "
                    f"(resume command v{command['version']})"
                )
        self.action_refresh()

    # --- display -----------------------------------------------------------------

    def _result(self, text: str) -> None:
        self.query_one("#task-result", Static).update(text)

    def action_refresh(self) -> None:
        table = self.query_one("#task-runs", DataTable)
        table.clear(columns=True)
        table.add_columns("run", "status", "steps", "updated")
        app = self.app
        if not isinstance(app, VouchApp) or app.workspace is None:
            return
        from vouch_agent.appservices.execution import ExecutionService

        for task_run in ExecutionService(app.workspace).runs():
            table.add_row(
                task_run.run_id,
                task_run.status.value,
                str(len(task_run.steps)),
                task_run.updated_at,
            )
        self._show_selected_detail()

    def _show_selected_detail(self) -> None:
        run_id = self._selected_run()
        detail = self.query_one("#task-detail", Static)
        if run_id is None:
            detail.update("")
            return
        import json as _json

        app = self.app
        if not isinstance(app, VouchApp) or app.workspace is None:
            return
        from vouch_agent.appservices.execution import ExecutionService

        service = ExecutionService(app.workspace)
        run = service.status(run_id)
        result = service.result(run_id)
        lines = [f"run {run_id}: {run.status.value if run else '?'}"]
        if result is not None:
            lines.append(f"conclusion: {result.conclusion[:200]}")
            if result.not_done_items:
                lines.append("not done: " + "; ".join(result.not_done_items))
            if result.artifact_refs:
                try:
                    payload = _json.loads(app.workspace.artifacts.get(result.artifact_refs[-1]))
                    lines.append(
                        "delivered: " + _json.dumps(payload, ensure_ascii=False)[:300]
                    )
                except Exception:
                    lines.append("delivered: non-JSON artifact")
        else:
            lines.append("in flight or no result yet — recoverable via Pause/Resume/Cancel")
        detail.update("\n".join(lines))
