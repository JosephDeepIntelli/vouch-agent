"""Runtime package: pinned-JAZ execution component (see ports.py for the v1 seam).

Public surface (implementation of the lead-owned ports):

* :class:`vouch_agent.runtime.jaz_engine.JazRuntime` / :class:`JazSession` —
  bounded JAZ worker sessions (scope binding, structured returns, nested
  invokes, budget/step/wall-clock enforcement).
* :class:`vouch_agent.runtime.scripted_backend.ScriptedBackend` — the
  deterministic never-networked fake model backend (a real ``jaz.llm.BaseLLM``).
* :mod:`vouch_agent.runtime.guards` — offline integrity (socket guard) and
  best-effort subprocess isolation (``worker_process`` child entry).
* :mod:`vouch_agent.runtime.fatal_errors` — the FatalError-category bridges
  that make fail-closed conditions un-catchable inside the JAZ tree.
"""
