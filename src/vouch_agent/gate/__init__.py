"""信闸 Vouch Gate — deterministic action gating (design §6).

Confidence signals rank, never authorize. The broker re-validates normalized
parameters, resource scope, budget reservation, policy digest and approval
content digest at execution time, so an approval cannot be swapped onto
different parameters after the fact.
"""

from vouch_agent.gate.broker import Authorization, CapabilityBroker, GatePolicy
from vouch_agent.gate.proposal import ActionProposal, ConfidenceSignal, RiskClass

__all__ = [
    "ActionProposal",
    "Authorization",
    "CapabilityBroker",
    "ConfidenceSignal",
    "GatePolicy",
    "RiskClass",
]
