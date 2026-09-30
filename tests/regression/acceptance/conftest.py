"""Regression-suite path setup.

The acceptance regressions reuse the CLI test helpers (``tests/cli/helpers``)
and share their own port fakes through ``support``; both live next to the
tests, so put this directory and ``tests/cli`` on ``sys.path`` the same way
pytest does for the suites that own them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CLI = _HERE.parents[1] / "cli"
for _entry in (str(_HERE), str(_CLI)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
