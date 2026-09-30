"""Adapters package: versioned process contracts to systems-under-improvement.

* :mod:`vouch_agent.adapters.protocol` — adapter protocol v1 (frames, transport)
* :mod:`vouch_agent.adapters.process_adapter` — AdapterClient over any v1 subprocess
* :mod:`vouch_agent.adapters.fixture_adapter` — SYNTHETIC in-repo Choose fixture adapter
"""

from vouch_agent.adapters.process_adapter import (
    ProcessAdapterClient,
    fixture_adapter_command,
)
from vouch_agent.adapters.protocol import (
    Frame,
    FrameKind,
    ProtocolTransport,
    SequenceTracker,
    parse_frame,
)

__all__ = [
    "Frame",
    "FrameKind",
    "ProcessAdapterClient",
    "ProtocolTransport",
    "SequenceTracker",
    "fixture_adapter_command",
    "parse_frame",
]
