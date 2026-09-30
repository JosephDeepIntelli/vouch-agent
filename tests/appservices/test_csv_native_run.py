"""CSV reconciliation as a native run: durable, materialized, exportable."""

from __future__ import annotations

import importlib.util as _il
import json
from pathlib import Path

_spec = _il.spec_from_file_location(
    "m3_runtime_helpers",
    Path(__file__).parents[1] / "regression" / "runtime" / "m3_runtime_helpers.py",
)
_m3 = _il.module_from_spec(_spec)
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

from vouch_agent.appservices.execution import ExecutionService  # noqa: E402
from vouch_agent.appservices.workspace import ProjectWorkspace  # noqa: E402

LEFT = "sku,name,price\nA-1,Kettle,95.00\nA-2,Grinder,129.00\n"
RIGHT = "sku,name,price\nA-1,Kettle,95.00\nA-2,Grinder,139.00\n"


def test_csv_reconciliation_native_run(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        run_id, report, digest = service.run_csv_reconciliation(
            goal="Reconcile the catalog export against the supplier feed",
            left_csv=LEFT.encode(),
            right_csv=RIGHT.encode(),
            join_key="sku",
        )
        assert report.changed and report.changed[0]["key"] == "A-2"
        status = service.status(run_id)
        assert status is not None and status.status.value == "completed"
        result = service.result(run_id)
        assert result is not None and result.deliverable()
        # the sealed report artifact is the digest-verified discrepancy report
        payload = workspace.artifacts.get(digest)
        parsed = json.loads(payload.decode("utf-8"))
        assert parsed["joinKey"] == "sku"
        assert parsed["rowCounts"] == {"left": 2, "right": 2, "matched": 2}
        # changing the data changes the result
        _, report_b, digest_b = service.run_csv_reconciliation(
            goal="reconcile again",
            left_csv=LEFT.encode(),
            right_csv=RIGHT.replace("139.00", "149.00").encode(),
            join_key="sku",
        )
        assert digest_b != digest
        assert report_b.changed[0]["differences"][0]["right"] == "149.00"
    finally:
        workspace.close()
