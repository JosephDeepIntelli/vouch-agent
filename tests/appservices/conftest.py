"""Path setup: reuse the runtime regression helpers (init_native_project)."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RUNTIME = _HERE.parents[1] / "regression" / "runtime"
for entry in (str(_HERE), str(_RUNTIME)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
