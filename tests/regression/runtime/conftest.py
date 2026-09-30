"""Regression-suite path setup.

The runtime regressions reuse the orchestrator test fakes (``tests/
orchestrator/fakes.py``) exactly like the coordinator reproductions did, and
the pinned-JAZ integration helpers (``tests/integration``); put both on
``sys.path`` the same way pytest does for the suites that own them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ORCHESTRATOR = _HERE.parents[1] / "orchestrator"
_INTEGRATION = _HERE.parents[1] / "integration"
for _entry in (str(_HERE), str(_ORCHESTRATOR), str(_INTEGRATION)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
