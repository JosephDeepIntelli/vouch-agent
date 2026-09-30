"""Structural boundary tests (lead-owned).

Enforces the trust-boundary and packaging rules that the JAZ ADR and design
doc §9/§13 make load-bearing:

1. The pinned JAZ runtime is exactly the reviewed version (upgrades require
   re-running Vouch contract tests — docs/jaz-reuse-decision.md).
2. Worker-side modules (runtime) never import privileged modules (gate,
   commit-side storage of acceptance labels, orchestrator policy).
3. Contracts stay import-dependency-free of implementation packages so other
   packages can depend on them without cycles.
"""

from __future__ import annotations

import ast
import importlib.metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "vouch_agent"

#: Modules reachable from worker (model-authored code) contexts.
WORKER_SIDE_PACKAGES = {"runtime"}

#: Modules that hold credentials, acceptance labels or policy.
PRIVILEGED_PACKAGES = {"gate", "orchestrator", "commit", "evaluation", "controller"}


def test_pinned_jaz_version_is_the_reviewed_one() -> None:
    assert importlib.metadata.version("jaz-lang") == "0.2.0a4", (
        "jaz-lang must stay pinned to 0.2.0a4 (commit 0803d497…, "
        "docs/jaz-reuse-decision.md); upgrades must re-run Vouch contract tests"
    )


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_worker_side_never_imports_privileged_modules() -> None:
    violations: list[str] = []
    for package in WORKER_SIDE_PACKAGES:
        for path in (SRC / package).rglob("*.py"):
            for module in _imports_of(path):
                if module.startswith("vouch_agent."):
                    second = module.split(".")[1] if "." in module else None
                    target = second or module
                    if second is None:
                        continue  # imports the package itself — checked below
                    if target in PRIVILEGED_PACKAGES or target == "storage":
                        violations.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not violations, "worker-side modules must not import privileged modules: " + ", ".join(
        violations
    )


def test_adapters_never_import_privileged_modules() -> None:
    violations: list[str] = []
    for path in (SRC / "adapters").rglob("*.py"):
        for module in _imports_of(path):
            if module.startswith(
                ("vouch_agent.controller", "vouch_agent.orchestrator", "vouch_agent.gate")
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not violations, "adapters must stay unprivileged: " + ", ".join(violations)


def test_contracts_import_no_implementation_packages() -> None:
    for path in (SRC / "contracts").rglob("*.py"):
        for module in _imports_of(path):
            assert not module.startswith(
                ("vouch_agent.gate", "vouch_agent.storage", "vouch_agent.runtime")
            ), f"{path.name} imports implementation package {module}"


@pytest.mark.parametrize(
    "package",
    ["contracts", "storage", "adapters", "runtime", "gate", "orchestrator", "cli"],
)
def test_declared_directories_exist_or_are_pending(package: str) -> None:
    # gate/orchestrator may not exist yet in early milestones; when they do,
    # they are inside src/vouch_agent with an __init__.py.
    directory = SRC / package
    if directory.exists():
        assert (directory / "__init__.py").exists(), f"{package} missing __init__.py"
