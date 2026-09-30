"""A deterministic, never-networked fake model backend for JAZ.

This is a REAL JAZ LLM backend — a subclass of :class:`jaz.llm.BaseLLM`, wired
through the public config seam (``jaz.configure(llm=...)`` /
``jaz.ConfigOverride(llm=...)``, and resolvable by tag through
``jaz.instantiate.build_component`` resolvers) — not a monkeypatched mock. It
consumes :class:`~vouch_agent.runtime.ports.WorkerSessionConfig`'s
``scripted_responses`` in order, across all invokes of a session (nested
sub-invokes draw from the same ordered pool), and returns proper
:class:`jaz.llm.LLMResponse` shapes with usage and cost.

Properties that matter for the Vouch trust model:

* **Never touches the network.** ``complete`` is pure: it derives deterministic
  token counts from the messages it was handed and books a configured fake
  cost. There is no HTTP client, no credential read, no env lookup.
* **Fails closed on exhaustion.** When the scripted pool runs out it raises
  :class:`~vouch_agent.runtime.fatal_errors.ReplayExhaustedFatal` (a
  ``FatalError``-category error), so the invoke tree terminates instead of
  the agent retrying around the failure. The base-class retry wrapper cannot
  re-drive it: the error is also listed in ``non_retryable_exceptions``.
* **Reports cost** — ``can_report_cost()`` is True so a ``BudgetPool`` with a
  cost budget is enforceable against the fake costs (the model id is not in
  JAZ's bundled price table, so the base's table-driven answer would be False).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

from jaz.llm import BaseLLM, LLMResponse

from vouch_agent.runtime.fatal_errors import ReplayExhaustedFatal

#: Backend tag for the data-driven instantiate seam (``llm={"backend": ...}``).
BACKEND_TAG = "vouch-scripted"

#: Default fake model id — deliberately not present in JAZ's price table so
#: accidental table pricing can never silently apply.
DEFAULT_MODEL_ID = "vouch-scripted/fixture-1"


class ScriptedBackend(BaseLLM):
    """Serve scripted responses as a real JAZ backend, offline and deterministic.

    Args:
        responses: The scripted model responses (Python code, as the code-only
            protocol expects), consumed in order and shared by every invoke in
            the session.
        cost_usd: Fake per-call cost booked on every response. Keep it
            positive when budget behaviour is under test so a ``BudgetPool``
            has something real to enforce.
        prompt_tokens_per_char / completion_tokens_per_char: Deterministic
            token-accounting factors (tokens ≈ chars / N). Only used to give
            usage() plausible, reproducible numbers.
        model: Fake model id reported to JAZ and in usage records.
    """

    def __init__(
        self,
        *,
        responses: Sequence[str] = (),
        cost_usd: float = 0.01,
        prompt_tokens_per_char: int = 4,
        completion_tokens_per_char: int = 4,
        model: str = DEFAULT_MODEL_ID,
    ) -> None:
        super().__init__(model=model, max_retries=0)
        self._responses: deque[str] = deque(responses)
        self.cost_usd = cost_usd
        self.prompt_tokens_per_char = max(1, prompt_tokens_per_char)
        self.completion_tokens_per_char = max(1, completion_tokens_per_char)
        self.served_calls = 0
        self.exhausted_at: int | None = None

    # --- configuration surface -------------------------------------------------

    @property
    def responses_remaining(self) -> int:
        """Scripted responses not yet served (across the whole session tree)."""
        return len(self._responses)

    def add_responses(self, responses: Sequence[str]) -> None:
        """Append scripted responses (used when a controller feeds a session)."""
        self._responses.extend(responses)

    @classmethod
    def from_dict(cls, params: dict[str, Any] | None) -> BaseLLM:
        return cls(**(params or {}))

    # --- BaseLLM contract --------------------------------------------------------

    @property
    def non_retryable_exceptions(self) -> tuple[type[BaseException], ...]:
        # Exhaustion must not be retried by the base tenacity wrapper: each
        # retry would either burn another scripted response or spin on the
        # same empty pool with exponential backoff.
        return (*super().non_retryable_exceptions, ReplayExhaustedFatal)

    def can_report_cost(self) -> bool:
        # The bundled price table has no entry for the fake model id; we book
        # our own cost, so a cost budget is enforceable.
        return True

    def complete(self, model: str, messages: list[Any], **kwargs: Any) -> LLMResponse:
        if not self._responses:
            self.exhausted_at = self.served_calls
            raise ReplayExhaustedFatal(
                f"scripted responses exhausted after {self.served_calls} call(s) "
                f"(model={model!r}); offline/fixture sessions fail closed here "
                "instead of falling back to a live call"
            )
        content = self._responses.popleft()
        self.served_calls += 1
        prompt_chars = 0
        for message in messages:
            if isinstance(message, dict):
                prompt_chars += len(str(message.get("content") or ""))
            else:
                prompt_chars += len(str(getattr(message, "content", None) or ""))
        return LLMResponse(
            content=content,
            prompt_tokens=prompt_chars // self.prompt_tokens_per_char,
            completion_tokens=len(content) // self.completion_tokens_per_char,
            cost_usd=self.cost_usd,
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic convenience
        return (
            f"ScriptedBackend(model={self.model!r}, remaining={self.responses_remaining}, "
            f"cost_usd={self.cost_usd!r})"
        )
