"""Record kinds introduced by the application services.

The controller's own kinds live in ``vouch_agent.controller.service``; these
extend the store with the mappings the CLI/TUI flows persist alongside them.
Keeping them in one tiny module lets flow and reporting share them without an
import cycle.
"""

from __future__ import annotations

#: workflow id -> {"versionId", "baselineRecordId"}
WORKFLOW_BASELINE_KIND = "workflow-baseline"
#: workflow id -> {"rubricDigest"}
WORKFLOW_RUBRIC_KIND = "workflow-rubric"
#: run id -> ComparisonSummary (the annotated paired comparison of a run)
COMPARISON_KIND = "comparison"
#: run id -> {"manifestDigest", "path"} of an exported evidence package
RUN_EXPORT_KIND = "run-export"
