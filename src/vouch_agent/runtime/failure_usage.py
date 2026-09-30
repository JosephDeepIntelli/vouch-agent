"""Measured usage that survives a failed step (review A2).

A failure's exception class says nothing about what was spent before the
failure: a replay-exhausted step may already have paid for two queries. This
module lets a runtime attach the usage it measured to the error it raises (or
to one rebuilt from a worker-process frame), so the orchestrator can settle
money that was actually spent instead of inferring "zero" from the taxonomy
name.

The attribute is deliberately a plain ``usage`` attribute on the exception
instance: the error classes live in the shared taxonomy
(:mod:`vouch_agent.errors`) and must not grow a runtime-specific constructor.
"""

from __future__ import annotations

import contextlib
from typing import Any


def attach_usage(exc: BaseException, usage: dict[str, Any]) -> BaseException:
    """Carry ``usage`` on ``exc`` so the failure path keeps its metering."""
    with contextlib.suppress(AttributeError, TypeError):  # exotic exception slots
        exc.usage = dict(usage)  # type: ignore[attr-defined]
    return exc


def usage_of(exc: BaseException) -> dict[str, Any] | None:
    """The usage attached to ``exc`` (a copy), or ``None`` when absent.

    Returns ``None`` for a *missing or empty* payload alike: "no usage
    survived this failure" and "usage survived but measured nothing" must be
    indistinguishable to callers, who treat both as unmeasured spend.
    """
    value = getattr(exc, "usage", None)
    if isinstance(value, dict) and value:
        return dict(value)
    return None
