"""Vouch CLI — headless commands over the shared application services.

Design §11: ``vouch init / baseline / propose / evaluate / review / export /
resume`` (+ status/version/modes and the accept/approve/release tail of the
improvement vertical). Every command is flag-driven (CI-able, no prompts),
works offline, and exits non-zero on fail-closed errors with the error
taxonomy code visible, e.g.::

    fail-closed [vouch/budget-exhausted]: cannot reserve ...

Commands never touch project state except through
:mod:`vouch_agent.appservices`; the TUI shares exactly the same layer.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

import vouch_agent
from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.flow import (
    ADAPTER_CHOOSE,
    ADAPTER_FIXTURE,
    ADAPTER_SCRIPTED,
    ImprovementFlow,
)
from vouch_agent.appservices.packs import import_choose_application_pack, import_fixture_pack
from vouch_agent.appservices.reporting import ReportService
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.candidate import CandidateState, ChangeType
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role
from vouch_agent.errors import ContractError, VouchError

app = typer.Typer(
    name="vouch",
    help="DeepIntelli Vouch — local agent runtime and improvement evidence tool.",
    no_args_is_help=True,
    add_completion=False,
)

ProjectOpt = Annotated[
    Path,
    typer.Option(
        "--project",
        "-p",
        help="Project directory (holds the .vouch/ workspace).",
        show_default=False,
    ),
]
WorkflowOpt = Annotated[
    str | None,
    typer.Option("--workflow", "-w", help="Workflow id (e.g. W-C3).", show_default=False),
]


def _fail_closed(exc: VouchError) -> NoReturn:
    typer.secho(f"fail-closed [{exc.code}]: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def catch_vouch[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    """Print fail-closed errors with their taxonomy code and exit 1."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return fn(*args, **kwargs)
        except VouchError as exc:
            _fail_closed(exc)

    return wrapper


def _workspace(project: Path) -> ProjectWorkspace:
    return ProjectWorkspace.open(project)


def _flow(project: Path) -> tuple[ProjectWorkspace, ImprovementFlow]:
    workspace = _workspace(project)
    _require_improvement_mode(workspace)
    return workspace, ImprovementFlow(workspace)


def _require_improvement_mode(workspace: ProjectWorkspace) -> None:
    """Improvement/approval work is refused in task-only workspaces.

    A task-only workspace deliberately has no workflows, owner identities or
    budget: the improvement vertical needs its own explicit setup — never
    manufactured identities or a silently weakened authorization.
    """
    if workspace.spec.mode == "task-only":
        raise ContractError(
            "this workspace is TASK-ONLY (native reconcile/inspect/export); "
            "improvement and approval commands are refused until a workspace "
            "is initialized in default mode with --workflow and named "
            "--owners (owner identities are never invented)"
        )


def _short(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        valid = ", ".join(role.value for role in Role)
        typer.secho(f"unknown role {value!r}; valid: {valid}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None


# --- informational commands ------------------------------------------------------


@app.command()
def version() -> None:
    """Print package version and pinned JAZ runtime version."""
    jaz_version = "unavailable"
    try:
        from importlib.metadata import version as pkg_version

        jaz_version = pkg_version("jaz-lang")
    except Exception:  # pragma: no cover - only hit with a broken install
        pass
    typer.echo(f"vouch-agent {vouch_agent.__version__} (jaz-lang {jaz_version})")


@app.command()
def modes() -> None:
    """List the distinguishable run modes and what this release ACTUALLY supports.

    The enum describes the identity discipline (design §13.2); the release
    support lines below are the honest capability statement: live execution
    is NOT supported in this preview.
    """
    from vouch_agent.contracts import RunMode

    for mode in RunMode:
        typer.echo(f"{mode.value}: live-calls={mode.allows_live_calls()}")
    typer.echo("release support: fixture (deterministic offline) SUPPORTED")
    typer.echo(
        "release support: offline-evaluation / authorized-live NOT SUPPORTED "
        "in this preview — no live model execution is implemented or verified"
    )


# --- native samples + deliverable verification ----------------------------------------

_SAMPLE_LEFT = """sku,name,price_usd,stock
AUR-001,Aurora Gooseneck Kettle,95.00,12
AUR-002,Aurora Travel Kettle,59.00,30
BRW-010,Oakline Pour-Over Brewer,42.00,8
MSC-100,Morning Scale,25.00,0
SLM-007,Summer Linen Set,120.00,4
"""

_SAMPLE_RIGHT = """sku,name,price_usd,stock,lead_days
AUR-001,Aurora Gooseneck Kettle,95.00,12,3
AUR-002,Aurora Travel Kettle,54.00,30,5
BRW-010,Oakline Pour-Over Brewer,42.00,8,4
MSC-100,Morning Scale,25.00,0,21
DNR-030,Dawn Mug Set,18.00,60,2
DNR-030,Dawn Mug Set,18.00,55,2
"""

_SAMPLE_README = """SYNTHETIC sample materials for the vouch native journey.
Every row is invented data; no real products, suppliers or prices.

  left  : "产品 目录.csv" (a product catalog; the filename deliberately
                           contains a space and Chinese characters)
  right : "supplier feed.csv" (a supplier feed, with a space too)

join key: sku

What the pair demonstrates:
- AUR-002: price changed (59.00 -> 54.00)
- SLM-007: present left only (missing from the supplier feed)
- DNR-030: present right only, TWICE (an ambiguous duplicate key)
- lead_days: a right-only column (reported as a schema difference)

Run (from the directory holding these files):
  vouch init --task-only --project ./vouch-work --purpose "supplier sync"
  vouch reconcile --project ./vouch-work \\
      --left "产品 目录.csv" --right "supplier feed.csv" --join-key sku
  vouch runs --project ./vouch-work
  vouch run-status --project ./vouch-work <run-id>
  vouch export-run --project ./vouch-work <run-id> --out ./vouch-work/export
  vouch verify-export ./vouch-work/export
"""


@app.command(name="examples")
@catch_vouch
def examples(
    out: Annotated[
        Path,
        typer.Option("--out", help="Directory to write the synthetic sample CSVs into."),
    ],
) -> None:
    """Write SYNTHETIC sample CSVs (spaces + Chinese filename) for the native journey."""
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "产品 目录.csv").write_text(_SAMPLE_LEFT, encoding="utf-8")
    (directory / "supplier feed.csv").write_text(_SAMPLE_RIGHT, encoding="utf-8")
    (directory / "README-examples.txt").write_text(_SAMPLE_README, encoding="utf-8")
    typer.echo(f"wrote: {directory / '产品 目录.csv'}")
    typer.echo(f"wrote: {directory / 'supplier feed.csv'}")
    typer.echo(f"wrote: {directory / 'README-examples.txt'}")
    typer.echo("SYNTHETIC: invented data; see README-examples.txt for the journey")


@app.command(name="verify-export")
@catch_vouch
def verify_export(
    directory: Annotated[
        Path, typer.Argument(help="Exported native run directory to verify.")
    ],
) -> None:
    """Verify an exported native run: manifest + every artifact digest, byte-level."""
    from vouch_agent.appservices.native_export import verify_native_export

    manifest = verify_native_export(Path(directory))
    artifacts = manifest.get("artifacts", [])
    typer.echo(
        f"verified export at {directory}: {len(artifacts)} artifact(s) match their digests"
    )
    for artifact in artifacts:
        typer.echo(f"  {artifact.get('file')}: {str(artifact.get('digest'))[:19]}…")


# --- direct task execution (Stage C) ---------------------------------------------


def _execution(project: Path) -> tuple[ProjectWorkspace, ExecutionService]:
    workspace = _workspace(project)
    return workspace, ExecutionService(workspace)


def _print_run(outcome: Any) -> None:
    typer.echo(f"run: {outcome.run_id} status: {outcome.status.value}")
    if outcome.error:
        typer.secho(f"error: {outcome.error}", fg=typer.colors.RED)
    result = outcome.result
    if result is not None:
        typer.echo(f"conclusion: {result.conclusion}")
        if result.done_items:
            typer.echo("done: " + "; ".join(result.done_items))
        if result.not_done_items:
            typer.secho("not done: " + "; ".join(result.not_done_items), fg=typer.colors.YELLOW)
        if result.uncertainties:
            typer.secho("uncertainties: " + "; ".join(result.uncertainties), fg=typer.colors.YELLOW)
        refs = result.artifact_refs
        if refs:
            for key, digest in zip(("inputs", "final"), (refs[0], refs[-1]), strict=False):
                if digest:
                    typer.echo(f"artifact {key}: {_short(digest)}")
        cost = result.total_cost_usd
        cost_text = "unmeasured" if cost is None else f"${cost:.4f}"
        typer.echo(f"cost: {cost_text} (fixture providers)")


@app.command()
@catch_vouch
def run(
    project: ProjectOpt,
    goal: Annotated[str, typer.Option("--goal", help="What the task must accomplish.")],
    input_file: Annotated[
        list[str] | None,
        typer.Option("--input", help="name=path.json task material (repeatable)."),
    ] = None,
    budget: Annotated[float, typer.Option("--budget")] = 0.5,
    max_steps: Annotated[int, typer.Option("--max-steps")] = 4,
    provider: Annotated[str, typer.Option("--provider")] = "extract-fact",
    script_file: Annotated[list[str] | None, typer.Option("--script-file")] = None,
    inline: Annotated[
        bool,
        typer.Option("--inline", help="In-process steps (ONLY shipped trusted sample scripts)."),
    ] = False,
    require_field: Annotated[
        list[str] | None,
        typer.Option(
            "--require-field",
            help="Final output must be a JSON object with this field (repeatable) "
            "— the machine-checkable completion condition.",
        ),
    ] = None,
    expect_text: Annotated[
        str, typer.Option("--expect-text", help="Final output must contain this text.")
    ] = "",
) -> None:
    """Execute a bounded task now and print the delivered result package."""
    from pathlib import Path as _P

    materials: dict[str, Any] = {}
    for token in input_file or []:
        name, sep, path = token.partition("=")
        if not sep or not name or not path:
            raise typer.BadParameter(f"--input expects name=path.json, got {token!r}")
        try:
            materials[name] = json.loads(_P(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"cannot read input {name!r}: {exc}") from exc
    if not materials:
        raise typer.BadParameter("at least one --input is required (task materials)")

    conditions: list[dict[str, Any]] = []
    if require_field:
        conditions.append(
            {
                "type": "artifact_schema",
                "schema": {
                    "type": "object",
                    "required": list(require_field),
                    "properties": {f: {"type": "string"} for f in require_field},
                },
            }
        )
    if expect_text:
        conditions.append({"type": "output_contains", "text": expect_text})
    if not conditions:
        typer.secho(
            "note: no --require-field/--expect-text given; completion is not "
            "machine-checkable and the run will stop honestly without them",
            fg=typer.colors.YELLOW,
        )

    workspace, service = _execution(project)
    outcome = service.run(
        goal=goal,
        inputs=materials,
        budget_usd=budget,
        max_steps=max_steps,
        provider=provider,
        script_files=tuple(script_file or []),
        isolated=not inline,
        completion_conditions=conditions,
    )
    result = outcome.result
    if result is not None and result.artifact_refs:
        final = result.artifact_refs[-1]
        try:
            payload = json.loads(workspace.artifacts.get(final).decode("utf-8"))
            preview = json.dumps(payload, ensure_ascii=False)
            if len(preview) > 400:
                preview = preview[:400] + "…"
            typer.echo(f"delivered: {preview}")
        except Exception:  # non-JSON artifact: name it, never dump bytes blindly
            typer.echo(f"delivered: non-JSON artifact {_short(final)}")
    typer.echo("execution: fixture providers, deterministic; not a live model")
    boundary = (
        "in-process (trusted sample scripts only)"
        if inline
        else "rlimit-bounded worker process, same OS user"
    )
    typer.echo(f"isolation: {boundary}")
    _print_run(outcome)
    if outcome.status.value != "completed":
        raise typer.Exit(1)


@app.command()
@catch_vouch
def run_detach(
    project: ProjectOpt,
    goal: Annotated[str, typer.Option("--goal")],
    input_file: Annotated[list[str] | None, typer.Option("--input", help="name=path.json")] = None,
    budget: Annotated[float, typer.Option("--budget")] = 0.5,
    max_steps: Annotated[int, typer.Option("--max-steps")] = 4,
    provider: Annotated[str, typer.Option("--provider")] = "extract-fact",
    script_file: Annotated[list[str] | None, typer.Option("--script-file")] = None,
    require_field: Annotated[list[str] | None, typer.Option("--require-field")] = None,
) -> None:
    """Submit, then execute in an INDEPENDENT worker process (client may exit).

    The worker owns the run with a fenced lease; explicit pause/resume
    commands (see `vouch run-status`) control continuation. Closing this
    client never kills or duplicates the work.
    """
    from pathlib import Path as _P

    from vouch_agent.appservices.worker import spawn_detached_worker

    materials: dict[str, Any] = {}
    for token in input_file or []:
        name, sep, path = token.partition("=")
        if not sep or not name or not path:
            raise typer.BadParameter(f"--input expects name=path.json, got {token!r}")
        materials[name] = json.loads(_P(path).read_text(encoding="utf-8"))
    if not materials:
        raise typer.BadParameter("at least one --input is required (task materials)")
    conditions = (
        [
            {
                "type": "artifact_schema",
                "schema": {
                    "type": "object",
                    "required": list(require_field),
                    "properties": {f: {"type": "string"} for f in require_field},
                },
            }
        ]
        if require_field
        else []
    )
    _, service = _execution(project)
    run_id = service.submit(
        goal=goal,
        inputs=materials,
        budget_usd=budget,
        max_steps=max_steps,
        completion_conditions=conditions,
    )
    if script_file:
        # seal the configuration synchronously (pause immediately) so the
        # detached worker loads the exact pool
        workspace, _ = _execution(project)
        from vouch_agent.appservices.execution import ExecutionService

        workspace.store.save("pause-request", run_id, {"requested": True})
        ExecutionService(workspace).execute(
            run_id, script_files=tuple(_P(f) for f in script_file or [])
        )
        workspace.close()
    pid = spawn_detached_worker(Path(project), run_id, provider=provider)
    typer.echo(f"detached run: {run_id} (worker pid {pid})")
    typer.echo("client may exit; reconnect with: vouch runs / vouch run-status / vouch export-run")


@app.command()
@catch_vouch
def export_run(
    project: ProjectOpt,
    run_id: Annotated[str, typer.Argument()],
    out: Annotated[Path, typer.Option("--out", help="Destination directory.")],
) -> None:
    """Export the FULL native ResultPackage (manifest + verified artifacts)."""
    from vouch_agent.appservices.worker import export_run as do_export

    workspace = _workspace(project)
    try:
        manifest = do_export(workspace, run_id, Path(out))
        typer.echo(f"exported: {manifest}")
    finally:
        workspace.close()


@app.command()
@catch_vouch
def reconcile(
    project: ProjectOpt,
    left: Annotated[Path, typer.Option("--left", help="Left CSV (e.g. product catalog).")],
    right: Annotated[Path, typer.Option("--right", help="Right CSV (e.g. supplier feed).")],
    join_key: Annotated[str, typer.Option("--join-key", help="Column joining both CSVs.")],
    delimiter: Annotated[str, typer.Option("--delimiter")] = ",",
    ignore_column: Annotated[list[str] | None, typer.Option("--ignore-column")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Optional export directory.")] = None,
) -> None:
    """Reconcile two CSVs deterministically; report + export the discrepancies."""
    from vouch_agent.appservices.worker import export_run as do_export

    left_bytes = Path(left).read_bytes()
    right_bytes = Path(right).read_bytes()
    workspace, service = _execution(project)
    try:
        run_id, report, digest = service.run_csv_reconciliation(
            goal=f"Reconcile {Path(left).name} vs {Path(right).name} on {join_key}",
            left_csv=left_bytes,
            right_csv=right_bytes,
            join_key=join_key,
            left_name=Path(left).name,
            right_name=Path(right).name,
            delimiter=delimiter,
            ignore_columns=tuple(ignore_column or []),
        )
        counts = report.to_dict()["rowCounts"]
        typer.echo(
            f"reconciled on {join_key}: {counts['matched']} matched, "
            f"{len(report.changed)} changed, {len(report.missing_left)} missing-left, "
            f"{len(report.missing_right)} missing-right, "
            f"{len(report.duplicate_keys)} duplicate keys"
        )
        typer.echo(f"run: {run_id} report digest: {_short(digest)}")
        if out is not None:
            manifest = do_export(workspace, run_id, Path(out))
            typer.echo(f"exported: {manifest}")
    finally:
        workspace.close()


@app.command()
@catch_vouch
def runs(project: ProjectOpt) -> None:
    """List task runs with status and step counts."""
    _, service = _execution(project)
    history = service.runs()
    if not history:
        typer.echo("no task runs yet — try: vouch run --goal ... --input fact=fact.json")
        return
    for task_run in history:
        typer.echo(
            f"{task_run.run_id}  {task_run.status.value:<12} "
            f"steps={len(task_run.steps)}  updated={task_run.updated_at}"
        )


@app.command()
@catch_vouch
def run_status(
    project: ProjectOpt,
    run_id: Annotated[str, typer.Argument(help="Run id from `vouch runs`.")],
) -> None:
    """Show one run: status, recovery classification, result summary."""
    _, service = _execution(project)
    task_run = service.status(run_id)
    if task_run is None:
        typer.secho(f"unknown run {run_id!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"status: {task_run.status.value}  steps: {len(task_run.steps)}")
    if task_run.error:
        typer.echo(f"error: {task_run.error}")
    report = service.recovery_report(run_id)
    typer.echo(f"recovery: {report.classification.value}  ({report.detail})")
    result = service.result(run_id)
    if result is not None:
        typer.echo(f"conclusion: {result.conclusion}")
        typer.echo(f"deliverable: {result.deliverable()}")


@app.command()
@catch_vouch
def run_cancel(
    project: ProjectOpt,
    run_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")] = "cancelled by operator",
) -> None:
    """Request cancellation (authoritative at the next step boundary)."""
    _, service = _execution(project)
    task_run = service.cancel(run_id, reason)
    if task_run is not None:
        typer.echo(f"run {run_id}: {task_run.status.value}")
    else:
        typer.secho(f"unknown run {run_id!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command()
@catch_vouch
def status(project: ProjectOpt) -> None:
    """Compact project + recovery posture overview."""
    workspace = _workspace(project)
    try:
        report = ReportService(workspace).review()
    finally:
        workspace.close()
    spec = workspace.spec
    typer.echo(f"project: {spec.project_id} ({spec.name})")
    typer.echo(f"workflows: {', '.join(w.workflow_id for w in spec.workflows)}")
    typer.echo(
        f"budget: cap ${report.costs.total_cap_usd:.2f} "
        f"measured ${report.costs.measured_usd:.4f} "
        f"reserved ${report.costs.outstanding_reserved_usd:.4f} "
        f"remaining ${report.costs.remaining_usd:.4f}"
    )
    typer.echo(
        f"candidates: {len(report.candidates)} runs: {len(report.runs)} "
        f"decisions: {len(report.decisions)} releases: {len(report.releases)}"
    )
    if report.resume.clean:
        typer.echo("recovery: clean (no open reservations, nothing needs reconciliation)")
    else:
        typer.secho(
            f"recovery: {len(report.resume.open_reservations)} open reservation(s), "
            f"{len(report.resume.needs_reconciliation)} subject(s) need reconciliation "
            "— run 'vouch resume'",
            fg=typer.colors.YELLOW,
        )


# --- project lifecycle -------------------------------------------------------------


def _parse_owner(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise typer.BadParameter(f"--owners expects role=name, got {value!r}")
    role, who = value.split("=", 1)
    role, who = role.strip(), who.strip()
    if not role or not who:
        raise typer.BadParameter(f"--owners expects role=name, got {value!r}")
    return role, who


@app.command()
@catch_vouch
def init(
    project: Annotated[
        Path,
        typer.Option("--project", "-p", help="Directory to create.", show_default=False),
    ],
    task_only: Annotated[
        bool,
        typer.Option(
            "--task-only",
            help=(
                "Initialize for the native task journey only (reconcile/inspect/"
                "export): no workflows, no owner identities, no improvement use."
            ),
        ),
    ] = False,
    purpose: Annotated[
        str,
        typer.Option("--purpose", help="Recorded purpose for the workspace (task-only)."),
    ] = "",
    workflow: Annotated[
        list[str] | None,
        typer.Option(
            "--workflow",
            "-w",
            help="Workflow id from the Choose manifest (W-C1..W-C9) or custom 'id=Name'.",
        ),
    ] = None,
    cap: Annotated[float, typer.Option("--cap", help="Total budget cap in USD.")] = 5.0,
    owners: Annotated[
        list[str] | None,
        typer.Option(
            "--owners",
            "-o",
            help="role=name (repeat; acceptance-owner and release-owner required).",
        ),
    ] = None,
    guardrail: Annotated[
        list[str] | None,
        typer.Option("--guardrail", "-g", help="Guardrail name applied to all workflows."),
    ] = None,
    change_type: Annotated[
        list[str] | None,
        typer.Option(
            "--change-type",
            help="Allowed candidate change types (default: all five v1 types).",
        ),
    ] = None,
    name: Annotated[str, typer.Option("--name", help="Project display name.")] = "",
    project_id: Annotated[str, typer.Option("--project-id", help="Project id.")] = "",
) -> None:
    """Create a vouch project directory and freeze its ProjectSpec.

    Task-only (--task-only) is the lightweight native mode: compare CSVs,
    inspect saved runs, export verified deliverables. Improvement work needs
    the default mode with workflows and named owners."""
    from vouch_agent.contracts.project import ProjectSpec, WorkflowDeclaration
    from vouch_agent.workflows import DECLARED_CHOOSE_WORKFLOWS

    workflow = list(workflow or ())
    if task_only:
        if workflow or owners:
            typer.secho(
                "--task-only takes no --workflow/--owners; improvement "
                "configuration belongs in a default-mode workspace",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        directory = Path(project)
        spec = ProjectSpec.from_dict(
            {
                "schemaVersion": "1",
                "projectId": project_id or f"proj-{directory.name.lower()}",
                "name": name or directory.name,
                "workflows": [],
                "owners": {},
                "allowedChangeTypes": [],
                # Local task runs still reserve ledger capacity as a guard;
                # nothing in task-only mode is paid or metered in USD.
                "budget": {"schemaVersion": "1", "totalUsdCap": cap},
                "mode": "task-only",
                "purpose": purpose,
            }
        )
        ProjectWorkspace.create(directory, spec)
        typer.echo(f"initialized {directory / '.vouch'}")
        typer.echo(f"project: {spec.project_id} ({spec.name}) mode: task-only")
        if purpose:
            typer.echo(f"purpose: {purpose}")
        typer.echo(
            f"reservation cap ${spec.budget.total_usd_cap:.2f} (local task "
            "reservations only; task-only work is never paid)"
        )
        typer.echo(
            "native journey: vouch examples --out samples && "
            "vouch reconcile --project . --left samples/… --right samples/… --join-key sku"
        )
        typer.echo(
            "improvement commands are refused in this workspace until it is "
            "re-initialized in default mode with workflows and owners"
        )
        return

    if not workflow:
        typer.secho(
            "pass --task-only for the native journey, or at least one "
            "--workflow for improvement mode",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    declared = {spec.workflow_id: spec for spec in DECLARED_CHOOSE_WORKFLOWS}
    declarations: list[WorkflowDeclaration] = []
    for token in workflow:
        if "=" in token:
            workflow_id, display = token.split("=", 1)
            objective = "recordedClaims"
        else:
            workflow_id, display = token, ""
            declared_spec = declared.get(workflow_id)
            if declared_spec is None:
                valid = ", ".join(sorted(declared))
                typer.secho(
                    f"unknown workflow {workflow_id!r}; declared: {valid} "
                    "(or use custom 'id=Name')",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            display = declared_spec.name
            objective = "recordedClaims"
        declarations.append(
            WorkflowDeclaration(
                workflow_id=workflow_id,
                name=display or workflow_id,
                main_objective=objective,
                guardrails=tuple(guardrail or ()),
            )
        )
    owner_map: dict[str, str] = {}
    for token in owners or []:
        role, who = _parse_owner(token)
        owner_map[role] = who
    missing = [r for r in ("acceptance-owner", "release-owner") if r not in owner_map]
    if missing:
        typer.secho(
            f"--owners must define {missing}; ownership is never implicit",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    allowed = tuple(change_type or ()) or tuple(ctype.value for ctype in ChangeType)
    directory = Path(project)
    display_name = name or directory.name
    spec = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": project_id or f"proj-{directory.name.lower()}",
            "name": display_name,
            "workflows": [w.to_dict() for w in declarations],
            "owners": owner_map,
            "allowedChangeTypes": list(allowed),
            "budget": {"schemaVersion": "1", "totalUsdCap": cap},
            "mode": "improvement",
            "purpose": purpose,
        }
    )
    ProjectWorkspace.create(directory, spec)
    typer.echo(f"initialized {directory / '.vouch'}")
    typer.echo(f"project: {spec.project_id} ({spec.name}) cap ${spec.budget.total_usd_cap:.2f}")
    for w in spec.workflows:
        typer.echo(
            f"frozen workflow {w.workflow_id}: {w.name} "
            f"(objective={w.main_objective}, guardrails={list(w.guardrails) or 'none'})"
        )
    typer.echo(f"allowed change types: {list(allowed)}")
    typer.echo(f"owners: { {role.value: who for role, who in spec.owners.items()} }")


@app.command()
@catch_vouch
def baseline(
    project: ProjectOpt,
    version: Annotated[str, typer.Option("--version", help="Baseline version id, e.g. v0.")],
    source_ref: Annotated[
        str, typer.Option("--source-ref", help="Reproducible source reference (git commit…).")
    ],
    workflow: WorkflowOpt = None,
    main_metric: Annotated[
        str | None,
        typer.Option(
            "--main-metric", help="Override the workflow's main metric.", show_default=False
        ),
    ] = None,
    min_improvement: Annotated[
        float,
        typer.Option(
            "--min-improvement",
            help="Min main-metric improvement (default 0 = non-inferiority).",
        ),
    ] = 0.0,
    min_complete_pairs: Annotated[
        int | None,
        typer.Option(
            "--min-complete-pairs", help="Minimum complete pairs for a conclusive verdict."
        ),
    ] = None,
    hard_guardrail: Annotated[
        list[str] | None,
        typer.Option(
            "--hard-guardrail",
            help="Hard guardrail name (repeat; default: the workflow's).",
        ),
    ] = None,
    repeats: Annotated[int, typer.Option("--repeats", help="Repeats per case and side.")] = 1,
    frozen_by: Annotated[
        str | None,
        typer.Option(
            "--frozen-by",
            help="Who freezes the standard (default: project acceptance owner).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Record the baseline AgentVersion and freeze the workflow rubric."""
    workspace, flow = _flow(project)
    try:
        result = flow.record_baseline(
            version_id=version,
            source_ref=source_ref,
            workflow_id=workflow,
            main_metric=main_metric,
            min_improvement=min_improvement,
            min_complete_pairs=min_complete_pairs,
            hard_guardrails=tuple(hard_guardrail) if hard_guardrail else None,
            repeats=repeats,
            frozen_by=frozen_by,
        )
    finally:
        workspace.close()
    rubric = result.rubric
    typer.echo(f"baseline: {result.baseline_id} (version {result.version.version_id})")
    typer.echo(
        f"rubric frozen: {_short(result.rubric_digest)} "
        f"(main={rubric.main_metric} {rubric.direction}, "
        f"min-improvement={rubric.thresholds.get('min-main-improvement', 0.0)}, "
        f"hard-guardrails={list(rubric.hard_guardrails) or 'none'}, repeats={rubric.repeats})"
    )
    typer.echo(f"frozen by: {rubric.frozen_by}")


# --- pack helper -------------------------------------------------------------------


@app.command()
@catch_vouch
def pack(
    project: ProjectOpt,
    from_fixture: Annotated[
        Path | None,
        typer.Option("--from-fixture", help="Fixture pack directory (synthetic)."),
    ] = None,
    from_choose_application: Annotated[
        bool,
        typer.Option(
            "--from-choose-application",
            help=(
                "Import the Choose runner's own application-scope case "
                "(requires the runner checkout; split via --application-split)."
            ),
        ),
    ] = False,
    application_split: Annotated[
        str,
        typer.Option(
            "--application-split",
            help=(
                "Split for --from-choose-application: "
                "development|selection-validation|final-acceptance."
            ),
        ),
    ] = "development",
    workflow: Annotated[
        str | None,
        typer.Option("--workflow", "-w", help="Workflow id, e.g. W-C3 (fixture packs)."),
    ] = None,
    dev: Annotated[int, typer.Option("--dev", help="Development-split scenarios.")] = 2,
    selection: Annotated[
        int, typer.Option("--selection", help="Selection-validation scenarios.")
    ] = 0,
    final: Annotated[int, typer.Option("--final", help="Final-acceptance scenarios.")] = 2,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Also write the pack JSON to this file.", show_default=False),
    ] = None,
) -> None:
    """Generate + import a SYNTHETIC task pack (fixture journeys or the Choose
    runner's application case)."""
    workspace = _require_improvement_workspace(project)
    try:
        if from_choose_application:
            if from_fixture is not None:
                typer.secho(
                    "--from-choose-application and --from-fixture are exclusive",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            try:
                split = CaseSplit(application_split)
            except ValueError:
                typer.secho(
                    f"unknown split {application_split!r}; use development, "
                    "selection-validation or final-acceptance",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1) from None
            task_pack = import_choose_application_pack(workspace, split=split)
        else:
            if from_fixture is None:
                typer.secho(
                    "pass --from-fixture <dir> or --from-choose-application",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            if not workflow:
                typer.secho(
                    "--workflow is required for fixture packs",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            task_pack = import_fixture_pack(
                workspace,
                Path(from_fixture),
                workflow,
                dev=dev,
                selection=selection,
                final=final,
            )
    finally:
        workspace.close()
    if out is not None:
        Path(out).write_text(
            json.dumps(task_pack.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    splits: dict[str, int] = {}
    for case in task_pack.cases:
        splits[case.split.value] = splits.get(case.split.value, 0) + 1
    typer.echo(f"pack: {task_pack.pack_id} (workflow {task_pack.workflow_id})")
    typer.echo("cases: " + ", ".join(f"{count} {split}" for split, count in sorted(splits.items())))
    typer.echo("SYNTHETIC: proves the pipeline only, never model improvement")
    if from_choose_application:
        typer.echo(
            "application scope: the Choose runner's apply-config case "
            f"({task_pack.cases[0].case_id}); candidate deltas apply through "
            "protocol v1.2, all-workflow fixture coverage stays with --adapter fixture"
        )
    if out is not None:
        typer.echo(f"written: {out}")


# --- evidence ingestion ---------------------------------------------------------------


def _require_improvement_workspace(project: Path) -> ProjectWorkspace:
    workspace = _workspace(project)
    _require_improvement_mode(workspace)
    return workspace


def _pack_ref_option(value: str | None, name: str) -> dict[str, str]:
    """Parse ``W-C6=pack-id`` mappings (repeatable)."""
    if value is None:
        return {}
    if "=" not in value:
        typer.secho(
            f"{name} must be WORKFLOW=PACK_ID, got {value!r}", err=True, fg=typer.colors.RED
        )
        raise typer.Exit(1)
    workflow_id, pack_id = value.split("=", 1)
    return {workflow_id.strip(): pack_id.strip()}


@app.command(name="evidence-import-browser")
@catch_vouch
def evidence_import_browser(
    project: ProjectOpt,
    export: Annotated[
        Path, typer.Option("--export", help="Choose browser-records.json export path.")
    ],
    pack: Annotated[
        list[str],
        typer.Option(
            "--pack",
            help="Workflow pack mapping WORKFLOW=PACK_ID (repeat; covers the export's workflows).",
        ),
    ],
    journey_case: Annotated[
        list[str] | None,
        typer.Option(
            "--journey-case",
            help=(
                "Journey case mapping JOURNEY=CASE_ID "
                "(repeat; a journey verifies those pack cases)."
            ),
        ),
    ] = None,
    screenshots: Annotated[
        Path | None,
        typer.Option("--screenshots", help="Screenshots dir (default: beside the export)."),
    ] = None,
    expected_commit: Annotated[
        str | None,
        typer.Option(
            "--expected-commit", help="Refuse unless the export tested exactly this commit."
        ),
    ] = None,
) -> None:
    """Import + validate a Choose browser-evidence export into durable records."""
    from vouch_agent.appservices.evidence_import import import_browser_evidence

    pack_refs: dict[str, str] = {}
    for mapping in pack:
        pack_refs.update(_pack_ref_option(mapping, "--pack"))
    journey_cases: dict[str, tuple[str, ...]] = {}
    for mapping in journey_case or ():
        if "=" not in mapping:
            typer.secho(
                f"--journey-case must be JOURNEY=CASE_ID, got {mapping!r}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        journey, case_id = mapping.split("=", 1)
        journey, case_id = journey.strip(), case_id.strip()
        journey_cases.setdefault(journey, ())
        journey_cases[journey] = (*journey_cases[journey], case_id)
    workspace = _require_improvement_workspace(project)
    try:
        result = import_browser_evidence(
            workspace,
            export_path=Path(export),
            pack_refs=pack_refs,
            journey_cases=journey_cases or None,
            screenshots_dir=Path(screenshots) if screenshots else None,
            expected_commit=expected_commit,
        )
    finally:
        workspace.close()
    typer.echo(f"tested source: {result.tested_commit}")
    typer.echo(f"journeys: {result.journeys} (screenshots verified: {result.screenshots_verified})")
    for record in result.records:
        typer.echo(
            f"record: {record.record_id} workflow={record.workflow_id} "
            f"status={record.status}"
        )
    typer.echo(
        "imported records are durable claims; coverage still resolves only against "
        "a matching source revision, pack and mode"
    )


@app.command(name="evidence-import-control")
@catch_vouch
def evidence_import_control(
    project: ProjectOpt,
    export: Annotated[
        Path, typer.Option("--export", help="Choose regression-records.json export path.")
    ],
    pack: Annotated[str, typer.Option("--pack", help="The W-C9 pack id.")],
    suite_case: Annotated[
        list[str] | None,
        typer.Option(
            "--suite-case",
            help=(
                "Family case mapping FAMILY=CASE_ID "
                "(repeat; regression-only, never optimization)."
            ),
        ),
    ] = None,
    expected_commit: Annotated[
        str | None,
        typer.Option(
            "--expected-commit", help="Refuse unless the export tested exactly this commit."
        ),
    ] = None,
) -> None:
    """Import + validate a Choose product-control regression export (W-C9)."""
    from vouch_agent.appservices.evidence_import import import_control_evidence

    suite_cases: dict[str, tuple[str, ...]] = {}
    for mapping in suite_case or ():
        if "=" not in mapping:
            typer.secho(
                f"--suite-case must be FAMILY=CASE_ID, got {mapping!r}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        family, case_id = mapping.split("=", 1)
        family, case_id = family.strip(), case_id.strip()
        suite_cases.setdefault(family, ())
        suite_cases[family] = (*suite_cases[family], case_id)
    workspace = _require_improvement_workspace(project)
    try:
        result = import_control_evidence(
            workspace,
            export_path=Path(export),
            pack_ref=pack,
            suite_cases=suite_cases or None,
            expected_commit=expected_commit,
        )
    finally:
        workspace.close()
    record = result.record
    typer.echo(f"tested source: {result.tested_commit}")
    typer.echo(f"suites: {result.suites}")
    typer.echo(
        f"record: {record.record_id} workflow={record.workflow_id} status={record.status}"
    )
    typer.echo("W-C9 stays regression-only: never an optimization target")


# --- pilot dry-run ---------------------------------------------------------------------


_GRADER_NOTE = (
    "frozen rubric + deterministic comparator; acceptance owner decides, "
    "release owner records; no model judges its own output"
)


@app.command(name="pilot-dryrun")
@catch_vouch
def pilot_dryrun(
    project: ProjectOpt,
    candidate: Annotated[str, typer.Option("--candidate", "-c", help="Sealed candidate id.")],
    pack: Annotated[str, typer.Option("--pack", help="Application-scope task pack id.")],
    provider_plan: Annotated[
        Path,
        typer.Option(
            "--provider-plan",
            help=(
                "Provider facts JSON (endpoint/model/credential NAME/prices/"
                "retries); SYNTHETIC placeholders are allowed and BLOCK the plan."
            ),
        ),
    ],
    owner_cap: Annotated[
        float | None,
        typer.Option(
            "--owner-cap",
            help="The owner's explicit total spend cap in USD (hard M5b gate).",
        ),
    ] = None,
    split: Annotated[
        str,
        typer.Option("--split", help="development|selection-validation|final-acceptance."),
    ] = "development",
    repeats: Annotated[int, typer.Option("--repeats", help="Paired repeats per case.")] = 1,
) -> None:
    """Package the M5b pilot as one no-network dry-run plan (no model calls)."""
    import json as _json

    from vouch_agent.appservices.pilot_dryrun import ProviderPlanInput, plan_pilot_dryrun

    try:
        raw = _json.loads(Path(provider_plan).read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        typer.secho(
            f"unreadable provider plan {provider_plan}: {exc}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1) from exc
    try:
        split_enum = CaseSplit(split)
    except ValueError:
        typer.secho(f"unknown split {split!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    workspace = _require_improvement_workspace(project)
    try:
        plan = plan_pilot_dryrun(
            workspace,
            candidate_id=candidate,
            pack_ref=pack,
            split=split_enum,
            repeats=repeats,
            provider=ProviderPlanInput.from_dict(raw),
            owner_cap_usd=owner_cap,
        )
    finally:
        workspace.close()
    color = typer.colors.GREEN if plan["status"] == "ready" else typer.colors.YELLOW
    typer.secho(f"pilot dry-run: {plan['status']}", fg=color, bold=True)
    for reason in plan["blockedReasons"]:
        typer.secho(f"  blocked: {reason}", fg=typer.colors.YELLOW)
    candidate_info = plan["candidate"]
    envelope = plan["requests"]["envelope"]
    typer.echo(
        f"candidate: {candidate_info['candidateId']} ({candidate_info['candidateDigest'][:19]}…)"
    )
    typer.echo(
        f"baseline: {candidate_info['baselineDigest'][:19]}…  "
        f"rubric: {plan['rubricDigest'][:19]}…"
    )
    material = plan["materialScope"]
    synthetic_note = " SYNTHETIC" if material["synthetic"] else ""
    typer.echo(
        f"material: workflow {plan['workflowId']} pack "
        f"{material['casePackDigest'][:19]}… ({len(material['caseIds'])} case(s), "
        f"split {material['split']}, repeats {material['repeats']}){synthetic_note}"
    )
    typer.echo(
        f"requests: {plan['requests']['planned']} planned = cases x 2 sides x "
        f"repeats x {envelope['maxModelRequestsPerAttempt']} max model requests "
        f"per attempt ({envelope['task']}, work budget v{envelope['version']}) "
        f"x (1 + {plan['requests']['maxRetriesPerRequest']} retries)"
    )
    typer.echo(
        f"credential: {plan['provider']['credentialName']} "
        "(name only; values stay in the owning service)"
    )
    modes = plan["providerModes"]
    typer.echo(
        f"provider modes: fixture={modes['fixture']['implemented']} "
        f"transport-simulation={modes['transportSimulation']['implemented']} "
        f"(loopback only) authorized-live={modes['authorizedLive']['implemented']}"
    )
    budget = plan["budget"]
    within = budget["withinCap"] if budget["withinCap"] is not None else "unknown"
    typer.echo(
        f"budget: worst case ${budget['worstCaseUsd']} / owner cap "
        f"{budget['ownerCapUsd'] if budget['ownerCapUsd'] is not None else 'NONE (M5b gate)'}"
        f" within cap: {within}"
        + (" [ESTIMATE ONLY]" if budget["estimateOnly"] else "")
    )
    evidence = plan.get("verifiedEvidence") or {}
    receipts = evidence.get("candidateAttempts") or []
    typer.echo(
        f"verified receipts: {len(receipts)} candidate attempt(s) bound to run "
        f"{evidence.get('runId', '—')}"
        + (f" (runner {evidence['runnerVersion']})" if evidence.get("runnerVersion") else "")
    )
    typer.echo(f"graders: {_GRADER_NOTE}")
    for rule in plan["stopRules"]:
        typer.echo(f"stop rule: {rule}")
    if plan["synthetic"]:
        typer.secho(
            "SYNTHETIC placeholders/materials present: this plan is a dry-run "
            "package, never permission to run",
            fg=typer.colors.YELLOW,
        )


# --- improvement vertical -------------------------------------------------------------


def _read_provider_transport(path: Path | None) -> dict | None:
    """Load the SIMULATION-ONLY provider-transport config (loopback fake)."""
    if path is None:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.secho(
            f"unreadable provider transport {path}: {exc}", err=True, fg=typer.colors.RED
        )
        raise typer.Exit(1) from exc
    if not isinstance(raw, dict):
        typer.secho(
            f"provider transport {path} must be a JSON object",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    return raw


def _read_delta(delta_file: str) -> str:
    if delta_file == "-":
        return sys.stdin.read()
    path = Path(delta_file)
    if not path.is_file():
        typer.secho(f"delta file {path} does not exist", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    return path.read_text(encoding="utf-8")


@app.command()
@catch_vouch
def propose(
    project: ProjectOpt,
    delta_file: Annotated[
        str,
        typer.Option("--delta-file", help="Path of the delta payload, or '-' for stdin."),
    ],
    type: Annotated[
        str,
        typer.Option("--type", help="Change type (one of the project's allowed types)."),
    ],
    rationale: Annotated[str, typer.Option("--rationale", help="Why this change, honestly.")],
    proposer: Annotated[
        str, typer.Option("--proposer", help="Proposer identity.")
    ] = "proposer-agent",
    expected_impact: Annotated[
        str, typer.Option("--expected-impact", help="Expected effect, stated up front.")
    ] = "",
    candidate_id: Annotated[
        str | None, typer.Option("--id", help="Explicit candidate id.", show_default=False)
    ] = None,
    workflow: WorkflowOpt = None,
    seal: Annotated[
        bool,
        typer.Option("--seal", help="Seal immediately (digest freeze); default prints a preview."),
    ] = False,
) -> None:
    """Propose a bounded change; validates scope and shows the seal preview."""
    delta = _read_delta(delta_file)
    if not delta.strip():
        typer.secho("delta is empty", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    workspace, flow = _flow(project)
    try:
        result = flow.propose(
            delta=delta,
            change_type=type,
            rationale=rationale,
            expected_impact=expected_impact,
            proposer=proposer,
            candidate_id=candidate_id,
            workflow_id=workflow,
            seal=seal,
        )
    finally:
        workspace.close()
    candidate = result.candidate
    typer.echo(f"candidate: {candidate.candidate_id}")
    typer.echo(
        f"change: {candidate.change_type.value} parent={candidate.parent_version.version_id}"
    )
    typer.echo(
        f"seal preview (content digest): {result.content_digest}"
        + ("" if seal else "  [not sealed; pass --seal to freeze]")
    )
    typer.echo(f"state: {candidate.state.value}")


@app.command()
@catch_vouch
def seal(
    project: ProjectOpt,
    candidate: Annotated[str, typer.Option("--candidate", "-c", help="Candidate id.")],
) -> None:
    """Seal a proposed candidate (digest freeze)."""
    workspace, flow = _flow(project)
    try:
        sealed = flow.seal(candidate)
    finally:
        workspace.close()
    typer.echo(f"sealed {sealed.candidate_id}: {sealed.content_digest()}")


@app.command()
@catch_vouch
def evaluate(
    project: ProjectOpt,
    candidate: Annotated[str, typer.Option("--candidate", "-c", help="Candidate id.")],
    pack: Annotated[
        str, typer.Option("--pack", help="Task pack: JSON file path or imported pack id.")
    ],
    split: Annotated[
        CaseSplit,
        typer.Option("--split", help="Evaluation split (development or selection-validation)."),
    ] = CaseSplit.DEVELOPMENT,
    adapter: Annotated[
        str,
        typer.Option(
            "--adapter",
            help=(
                "scripted (deterministic offline), fixture (subprocess), or "
                "choose (the REAL Choose runner; deterministic providers)."
            ),
        ),
    ] = ADAPTER_SCRIPTED,
    workflow: WorkflowOpt = None,
    rubric: Annotated[
        str | None,
        typer.Option("--rubric", help="Rubric digest override.", show_default=False),
    ] = None,
    repeats: Annotated[
        int | None, typer.Option("--repeats", help="Repeats (default: the frozen rubric's).")
    ] = None,
    role: Annotated[
        str, typer.Option("--role", help="Acting role for data visibility.")
    ] = Role.EVALUATOR.value,
    fixtures: Annotated[
        Path | None,
        typer.Option(
            "--fixtures", help="Fixture pack dir for --adapter fixture.", show_default=False
        ),
    ] = None,
    provider_transport: Annotated[
        Path | None,
        typer.Option(
            "--provider-transport",
            help=(
                "SIMULATION ONLY: a choose-provider-config JSON (loopback fake "
                "provider) the runner routes model requests through; never "
                "live-model authorization."
            ),
            show_default=False,
        ),
    ] = None,
    reserve: Annotated[
        float, typer.Option("--reserve", help="Per-attempt budget reservation (USD).")
    ] = 0.05,
) -> None:
    """Run the paired baseline-vs-candidate evaluation on a split."""
    workspace, flow = _flow(project)
    try:
        outcome = flow.evaluate(
            candidate_id=candidate,
            pack_ref=pack,
            split=split,
            adapter_kind=adapter,
            workflow_id=workflow,
            rubric_digest=rubric,
            repeats=repeats,
            role=_role(role),
            fixtures_dir=fixtures,
            per_attempt_reserve_usd=reserve,
            provider_transport=_read_provider_transport(provider_transport),
        )
    finally:
        workspace.close()
    _print_evaluation(outcome, fixture=adapter in (ADAPTER_FIXTURE, ADAPTER_CHOOSE))


def _print_evaluation(outcome: Any, *, fixture: bool) -> None:
    summary = outcome.summary
    typer.echo(
        f"run: {outcome.run.run_id} split={outcome.run.split.value} adapter={outcome.adapter_id}"
    )
    typer.echo(
        f"pairs: {summary.complete_pairs}/{summary.total_pairs} complete "
        f"(incomplete: {summary.incomplete_pairs})"
    )
    for metric, delta in sorted(summary.metric_deltas.items()):
        typer.echo(
            f"metric {metric}: baseline {delta.mean_baseline:.4f} -> candidate "
            f"{delta.mean_candidate:.4f} (delta {delta.mean_delta:+.4f}, n={delta.n_pairs})"
        )
    if summary.hard_violations:
        typer.secho("hard guardrail violations:", fg=typer.colors.RED)
        for finding in summary.hard_violations:
            typer.secho(f"  - {finding.name}: {finding.detail}", fg=typer.colors.RED)
    if summary.soft_violations:
        typer.echo("soft guardrail findings:")
        for finding in summary.soft_violations:
            typer.echo(f"  - {finding.name}: {finding.detail}")
    cost = summary.cost
    if cost is not None and cost.measurable and cost.total_usd is not None:
        typer.echo(
            f"cost: baseline ${cost.baseline_usd or 0:.4f} "
            f"candidate ${cost.candidate_usd or 0:.4f} "
            f"total ${cost.total_usd:.4f}"
        )
    else:
        typer.secho("cost: immeasurable (unpriced attempts)", fg=typer.colors.YELLOW)
    verdict_color = {
        "accepted": typer.colors.GREEN,
        "rejected": typer.colors.RED,
        "inconclusive": typer.colors.YELLOW,
    }[outcome.verdict.value]
    typer.secho(f"verdict: {outcome.verdict.value}", fg=verdict_color, bold=True)
    if outcome.advanced:
        typer.echo("candidate advanced to 'evaluated' (selection verdict accepted)")
    elif outcome.run.split in (CaseSplit.DEVELOPMENT, CaseSplit.SELECTION_VALIDATION):
        typer.echo(f"candidate stays '{outcome.candidate.state.value}' (verdict not accepted)")
    typer.echo(f"uncertainty: {summary.uncertainty_note}")
    typer.echo(f"mode: {outcome.run.mode.value}")
    if fixture:
        typer.echo(
            "note: fixture mode replays synthetic journeys; an accepted verdict is "
            "non-inferiority on synthetic data, never evidence of model improvement"
        )


@app.command()
@catch_vouch
def accept(
    project: ProjectOpt,
    candidate: Annotated[
        str, typer.Option("--candidate", "-c", help="Candidate id (must be evaluated).")
    ],
    pack: Annotated[str, typer.Option("--pack", help="Task pack with final-acceptance cases.")],
    out: Annotated[Path, typer.Option("--out", help="Evidence package destination directory.")],
    owner: Annotated[
        str,
        typer.Option(
            "--owner",
            help="Acceptance owner identity (never the proposer).",
        ),
    ],
    adapter: Annotated[str, typer.Option("--adapter")] = ADAPTER_SCRIPTED,
    workflow: WorkflowOpt = None,
    fixtures: Annotated[Path | None, typer.Option("--fixtures", show_default=False)] = None,
    provider_transport: Annotated[
        Path | None,
        typer.Option(
            "--provider-transport",
            help=(
                "SIMULATION ONLY: a choose-provider-config JSON (loopback fake "
                "provider) the runner routes model requests through."
            ),
            show_default=False,
        ),
    ] = None,
    reserve: Annotated[float, typer.Option("--reserve")] = 0.05,
) -> None:
    """Run final acceptance, export evidence, record the decision (anchored)."""
    workspace, flow = _flow(project)
    try:
        # Validate the owner identity BEFORE spending the final-acceptance
        # budget: the configured acceptance owner, never the candidate's own
        # proposer. Local role selection is an accountability label, not an
        # authenticated principal — stated plainly, no SaaS login.
        flow.check_acceptance_owner(candidate, owner)
        outcome = flow.final_acceptance(
            candidate_id=candidate,
            pack_ref=pack,
            adapter_kind=adapter,
            workflow_id=workflow,
            role=Role.ACCEPTANCE_OWNER,
            fixtures_dir=fixtures,
            per_attempt_reserve_usd=reserve,
        )
        _print_evaluation(outcome, fixture=adapter in (ADAPTER_FIXTURE, ADAPTER_CHOOSE))
        decision = flow.record_decision(outcome, owner=owner, evidence_destination=Path(out))
    finally:
        workspace.close()
    typer.echo(
        f"decision: {decision.decision.decision_id} verdict={decision.decision.verdict.value}"
    )
    typer.echo(f"evidence: {decision.package.path}")
    typer.echo(f"manifest digest: {decision.package.manifest_digest}")
    typer.echo(f"candidate state: {decision.candidate.state.value}")
    typer.echo(
        f"owner: {decision.decision.owner} (local accountability label; not an "
        "authenticated principal)"
    )


@app.command()
@catch_vouch
def approve(
    project: ProjectOpt,
    candidate: Annotated[str, typer.Option("--candidate", "-c")],
    role: Annotated[str, typer.Option("--role")] = Role.RELEASE_OWNER.value,
) -> None:
    """Record the release owner's approval (bound to the decision's digests)."""
    workspace, flow = _flow(project)
    try:
        approved, decision = flow.approve(candidate, role=_role(role))
    finally:
        workspace.close()
    typer.echo(f"approved {approved.candidate_id} (decision {decision.decision_id})")
    typer.echo(f"binding: {approved.content_digest()}")


@app.command()
@catch_vouch
def release(
    project: ProjectOpt,
    candidate: Annotated[str, typer.Option("--candidate", "-c")],
    deployed_version: Annotated[
        str, typer.Option("--deployed-version", help="What the product actually deployed.")
    ],
    deployed_by: Annotated[
        str, typer.Option("--deployed-by", help="Who ran the product's release process.")
    ],
    observed_window: Annotated[
        str, typer.Option("--observed-window", help="Post-release observation window.")
    ] = "",
) -> None:
    """Record the actual release event (Vouch never deploys itself)."""
    workspace, flow = _flow(project)
    try:
        released, record = flow.record_release(
            candidate,
            deployed_version=deployed_version,
            deployed_by=deployed_by,
            observed_window=observed_window,
        )
    finally:
        workspace.close()
    typer.echo(f"released {released.candidate_id} via {record.release_id}")
    typer.echo(f"deployed: {record.deployed_version} by {record.deployed_by}")


# --- review / export / recovery -------------------------------------------------------


@app.command()
@catch_vouch
def review(project: ProjectOpt) -> None:
    """Pending decisions, candidate states, guardrail findings, costs."""
    workspace = _workspace(project)
    try:
        report = ReportService(workspace).review()
    finally:
        workspace.close()
    typer.echo(f"project: {report.project_id} ({report.project_name})")
    typer.echo("")
    typer.echo("candidates:")
    if not report.candidates:
        typer.echo("  (none)")
    for candidate in report.candidates:
        marker = (
            "*"
            if candidate.state
            in (
                CandidateState.PROPOSED,
                CandidateState.SEALED,
                CandidateState.EVALUATED,
                CandidateState.ACCEPTED,
                CandidateState.APPROVED,
            )
            else " "
        )
        typer.echo(
            f"{marker} {candidate.candidate_id}: {candidate.state.value} "
            f"[{candidate.change_type.value} by {candidate.proposer}] "
            f"digest={_short(candidate.content_digest())}"
        )
    typer.echo("")
    typer.echo("pending decisions / next expected action:")
    if not report.pending:
        typer.echo("  (nothing pending)")
    for candidate, action in report.pending:
        typer.echo(f"  - {candidate.candidate_id} ({candidate.state.value}): {action}")
    typer.echo("")
    typer.echo("decisions:")
    if not report.decisions:
        typer.echo("  (none)")
    for decision in report.decisions:
        typer.echo(
            f"  - {decision.decision_id}: {decision.verdict.value} by {decision.owner} "
            f"evidence={_short(decision.evidence_digest)}"
        )
    typer.echo("")
    if report.hard_findings:
        typer.secho("hard guardrail findings:", fg=typer.colors.RED)
        for run_id, finding in report.hard_findings:
            typer.secho(f"  - {run_id}: {finding.name}: {finding.detail}", fg=typer.colors.RED)
    else:
        typer.echo("hard guardrail findings: none")
    if report.soft_findings:
        typer.echo("soft guardrail findings:")
        for run_id, finding in report.soft_findings:
            typer.echo(f"  - {run_id}: {finding.name}: {finding.detail}")
    typer.echo("")
    costs = report.costs
    typer.echo(
        f"costs: measured ${costs.measured_usd:.4f} / cap ${costs.total_cap_usd:.2f} "
        f"(reserved ${costs.outstanding_reserved_usd:.4f}, remaining "
        f"${costs.remaining_usd:.4f}, human {costs.human_minutes:.0f}min)"
    )
    if costs.unmeasurable_entries:
        typer.secho(
            f"unmeasurable cost entries: {len(costs.unmeasurable_entries)} "
            "(recorded, never rounded to zero)",
            fg=typer.colors.YELLOW,
        )
    typer.echo("")
    coverage = report.coverage
    typer.echo(
        "workflow coverage: "
        + ", ".join(f"{len(ids)} {status}" for status, ids in sorted(coverage.items()))
        + " (fixture-covered proves the pipeline only; runner-integrated needs a real runner)"
    )


@app.command()
@catch_vouch
def export(
    project: ProjectOpt,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Evaluation run id (evidence package).", show_default=False),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Destination directory (must be empty/new).", show_default=False
        ),
    ] = None,
    rollback: Annotated[
        bool,
        typer.Option("--rollback", help="Export a rollback plan for --candidate instead."),
    ] = False,
    candidate: Annotated[
        str | None,
        typer.Option(
            "--candidate", "-c", help="Candidate id (for --rollback).", show_default=False
        ),
    ] = None,
) -> None:
    """Export the deterministic evidence package (or rollback plan)."""
    workspace = _workspace(project)
    try:
        service = ReportService(workspace)
        if rollback:
            if candidate is None:
                typer.secho("--rollback requires --candidate", err=True, fg=typer.colors.RED)
                raise typer.Exit(1)
            if out is None:
                typer.secho("--rollback requires --out", err=True, fg=typer.colors.RED)
                raise typer.Exit(1)
            package = service.export_rollback(candidate, destination=out)
            kind = "rollback-plan"
        else:
            if run is None or out is None:
                typer.secho(
                    "evidence export requires --run and --out", err=True, fg=typer.colors.RED
                )
                raise typer.Exit(1)
            package = service.export_run(run, destination=out)
            kind = package.package_kind
    finally:
        workspace.close()
    typer.echo(f"package: {kind} -> {package.path}")
    typer.echo(f"files: {len(package.file_digests)} mode: {package.mode.value}")
    typer.echo(f"manifest digest: {package.manifest_digest}")


@app.command()
@catch_vouch
def resume(
    project: ProjectOpt,
    reconcile: Annotated[
        str | None,
        typer.Option(
            "--reconcile",
            help="Subject to reconcile (run id or stale reservation id).",
            show_default=False,
        ),
    ] = None,
    verified_note: Annotated[
        str,
        typer.Option("--verified-note", help="What was actually verified (query/human check)."),
    ] = "",
    settle: Annotated[
        float | None,
        typer.Option("--settle", help="With --reconcile <reservation>: book verified USD spend."),
    ] = None,
    release_hold: Annotated[
        bool,
        typer.Option("--release", help="With --reconcile <reservation>: return an unspent hold."),
    ] = False,
) -> None:
    """Show recovery state; reconcile explicitly with a verified note."""
    workspace, flow = _flow(project)
    try:
        if reconcile is None:
            state = ReportService(workspace).resume_state()
            typer.echo(f"journal events: {state.journal_events}")
            if state.open_reservations:
                typer.secho(
                    "open budget reservations (stale = crash evidence):", fg=typer.colors.YELLOW
                )
                for reservation in state.open_reservations:
                    typer.echo(
                        f"  - {reservation.reservation_id}: ${reservation.amount_usd:.4f} "
                        f"for {reservation.holder} (created {reservation.created_at})"
                    )
            else:
                typer.echo("open budget reservations: none")
            if state.needs_reconciliation:
                typer.secho("subjects needing reconciliation:", fg=typer.colors.YELLOW)
                for subject in state.needs_reconciliation:
                    typer.echo(f"  - {subject}")
            else:
                typer.echo("needs-reconciliation subjects: none")
            if state.open_reservations or state.needs_reconciliation:
                typer.echo(
                    "reconcile explicitly: vouch resume --project … --reconcile <id> "
                    '--verified-note "what was verified" [--settle USD | --release]'
                )
            else:
                typer.echo("recovery: clean — deterministic offline work may resume")
            return
        actions = flow.reconcile(
            reconcile,
            verified_note,
            settle_usd=settle,
            release=release_hold,
        )
    finally:
        workspace.close()
    typer.echo(f"reconciled {reconcile}: {actions}")


@app.command()
def tui(
    project: Annotated[
        Path | None,
        typer.Option(
            "--project", "-p", help="Project directory to open at startup.", show_default=False
        ),
    ] = None,
) -> None:
    """Open the interactive TUI (a disconnectable client of the app services)."""
    from vouch_agent.tui import run_tui

    run_tui(str(project) if project is not None else None)


if __name__ == "__main__":  # pragma: no cover
    app()
