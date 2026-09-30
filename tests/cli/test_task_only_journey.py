"""The RC1 native journey: task-only init -> reconcile -> inspect -> export/verify.

The supported public contract must work without workflows, owner identities,
a Choose checkout or credentials, from any directory, with spaces/Chinese
filenames — and improvement/approval commands must be REFUSED in that
workspace rather than silently weakened. Legacy improvement workspaces keep
working unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from vouch_agent.cli.main import app

runner = CliRunner()


def _journey(tmp_path: Path) -> str:
    """init -> examples -> reconcile; returns the run id."""
    project = tmp_path / "vouch-work"
    result = runner.invoke(
        app,
        [
            "init",
            "--task-only",
            "--project",
            str(project),
            "--purpose",
            "supplier sync preview",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mode: task-only" in result.output
    assert "purpose: supplier sync preview" in result.output

    samples = tmp_path / "samples"
    examples = runner.invoke(app, ["examples", "--out", str(samples)])
    assert examples.exit_code == 0, examples.output
    assert "SYNTHETIC: invented data" in examples.output
    assert (samples / "产品 目录.csv").is_file()
    assert (samples / "supplier feed.csv").is_file()

    reconciled = runner.invoke(
        app,
        [
            "reconcile",
            "--project",
            str(project),
            "--left",
            str(samples / "产品 目录.csv"),
            "--right",
            str(samples / "supplier feed.csv"),
            "--join-key",
            "sku",
        ],
    )
    assert reconciled.exit_code == 0, reconciled.output
    assert "1 changed" in reconciled.output
    assert "1 missing-left" in reconciled.output
    assert "1 missing-right" in reconciled.output
    assert "1 duplicate keys" in reconciled.output
    return reconciled.output.split("run: ")[1].split()[0]


def _export_and_verify(project: Path, run_id: str) -> Path:
    out = project / "export"
    exported = runner.invoke(
        app, ["export-run", "--project", str(project), run_id, "--out", str(out)]
    )
    assert exported.exit_code == 0, exported.output
    verified = runner.invoke(app, ["verify-export", str(out)])
    assert verified.exit_code == 0, verified.output
    assert "verified export" in verified.output
    return out


class TestTaskOnlyJourney:
    def test_full_journey_with_spaces_and_chinese_filenames(self, tmp_path: Path) -> None:
        project = tmp_path / "vouch-work"
        run_id = _journey(tmp_path)

        listed = runner.invoke(app, ["runs", "--project", str(project)])
        assert listed.exit_code == 0, listed.output
        assert run_id in listed.output
        detailed = runner.invoke(app, ["run-status", "--project", str(project), run_id])
        assert detailed.exit_code == 0, detailed.output
        assert "completed" in detailed.output

        out = _export_and_verify(project, run_id)
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["kind"] == "vouch-native-run-export"

    def test_changed_input_changes_the_result(self, tmp_path: Path) -> None:
        project = tmp_path / "vouch-work"
        first_run = _journey(tmp_path)
        first_export = _export_and_verify(project, first_run)
        first_manifest = (first_export / "manifest.json").read_bytes()

        # Sensitivity: a changed input row must produce a DIFFERENT run and
        # different deliverable bytes (immutable input snapshots bind input).
        right = tmp_path / "samples" / "supplier feed.csv"
        right.write_text(
            right.read_text(encoding="utf-8").replace("54.00", "53.00"), encoding="utf-8"
        )
        again = runner.invoke(
            app,
            [
                "reconcile",
                "--project",
                str(project),
                "--left",
                str(tmp_path / "samples" / "产品 目录.csv"),
                "--right",
                str(right),
                "--join-key",
                "sku",
            ],
        )
        assert again.exit_code == 0, again.output
        second_run = again.output.split("run: ")[1].split()[0]
        assert second_run != first_run
        out2 = project / "export-2"
        exported2 = runner.invoke(
            app, ["export-run", "--project", str(project), second_run, "--out", str(out2)]
        )
        assert exported2.exit_code == 0, exported2.output
        assert runner.invoke(app, ["verify-export", str(out2)]).exit_code == 0
        assert (out2 / "manifest.json").read_bytes() != first_manifest

    def test_tampered_export_fails_verification(self, tmp_path: Path) -> None:
        project = tmp_path / "vouch-work"
        run_id = _journey(tmp_path)
        out = _export_and_verify(project, run_id)
        artifact = next(path for path in out.iterdir() if path.name != "manifest.json")
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        broken = runner.invoke(app, ["verify-export", str(out)])
        assert broken.exit_code != 0

    def test_improvement_commands_are_refused_in_task_only(self, tmp_path: Path) -> None:
        project = tmp_path / "vouch-work"
        _journey(tmp_path)
        (tmp_path / "delta.txt").write_text("bounded change", encoding="utf-8")
        (tmp_path / "pp.json").write_text("{}", encoding="utf-8")
        for args in (
            ["baseline", "--project", str(project), "--version", "v0", "--source-ref", "x"],
            ["propose", "--project", str(project), "--type", "prompt-delta",
             "--rationale", "x", "--delta-file", str(tmp_path / "delta.txt")],
            ["evaluate", "--project", str(project), "--candidate", "cand-x",
             "--pack", "p", "--adapter", "scripted"],
            ["accept", "--project", str(project), "--candidate", "cand-x", "--pack", "p",
             "--out", str(tmp_path / "e"), "--owner", "ana"],
            ["approve", "--project", str(project), "--candidate", "cand-x"],
            ["release", "--project", str(project), "--candidate", "cand-x",
             "--deployed-version", "v1", "--deployed-by", "x"],
            ["pack", "--project", str(project), "--from-fixture", str(tmp_path),
             "--workflow", "W-C2"],
            ["pilot-dryrun", "--project", str(project), "--candidate", "cand-x",
             "--pack", "p", "--provider-plan", str(tmp_path / "pp.json")],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code != 0, args
            assert "TASK-ONLY" in result.output, args

    def test_legacy_improvement_workspace_still_works(self, tmp_path: Path) -> None:
        delta = tmp_path / "delta.txt"
        delta.write_text("bounded change", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                str(tmp_path / "imp"),
                "--workflow",
                "W-C2",
                "--owners",
                "acceptance-owner=ana",
                "--owners",
                "release-owner=roger",
                "--cap",
                "2.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "frozen workflow W-C2" in result.output
        baseline = runner.invoke(
            app,
            [
                "baseline",
                "--project",
                str(tmp_path / "imp"),
                "--version",
                "v0",
                "--source-ref",
                "git:choose",
                "--main-metric",
                "supportedClaims",
            ],
        )
        assert baseline.exit_code == 0, baseline.output

    def test_task_only_init_refuses_improvement_mix(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "init",
                "--task-only",
                "--project",
                str(tmp_path / "mix"),
                "--workflow",
                "W-C2",
            ],
        )
        assert result.exit_code != 0
        assert "no --workflow/--owners" in result.output

    def test_fresh_process_reopens_the_workspace(self, tmp_path: Path) -> None:
        project = tmp_path / "vouch-work"
        run_id = _journey(tmp_path)
        out = _export_and_verify(project, run_id)
        code = r"""
import sys
sys.path.insert(0, "__SRC__")
from pathlib import Path
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.appservices.native_export import verify_native_export
w = ProjectWorkspace.open(Path("__PROJECT__"))
spec = w.spec
assert spec.mode == "task-only", spec.mode
assert spec.purpose == "supplier sync preview"
runs = w.store.list_ids("task-run")
assert runs, "no durable task runs"
manifest = verify_native_export(Path("__EXPORT__"))
print("REOPEN_OK", spec.mode, len(runs), len(manifest["artifacts"]))
"""
        code = (
            code.replace("__SRC__", str(Path(__file__).parents[2] / "src"))
            .replace("__PROJECT__", str(project))
            .replace("__EXPORT__", str(out))
        )
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("REOPEN_OK task-only"), proc.stdout
