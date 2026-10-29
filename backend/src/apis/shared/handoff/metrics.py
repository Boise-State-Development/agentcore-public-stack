"""Handoff EMF counts (``docs/specs/agent-handoffs-and-consults.md`` §4.7).

Under the PromptCache namespace, so the cost dashboard can sit them beside the cache numbers. The
same facts are on the ``HANDOFF#`` rows; these are the fleet-wide view the §7 gate reads.
Best-effort: never raises.
"""

from __future__ import annotations

from typing import Optional

from apis.shared.observability.emf import emit_emf_metrics

_NAMESPACE = "AgentCoreStack/PromptCache"


def emit_handoff_metric(
    name: str,
    kind: str,
    *,
    session_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """One count of ``name`` (``HandoffCreated``, ``HandoffCancelled`` …), with ``kind`` as a property.

    ``detail`` rides as a property too: the interrupt kind on ``HandoffNeedsInput``, the reason on
    ``HandoffStopped``. Properties, not dimensions, like every other caller of ``emit_emf_metrics``.
    """
    emit_emf_metrics(
        _NAMESPACE,
        metrics={name: 1},
        properties={"handoffKind": kind, "sessionId": session_id, "handoffDetail": detail},
        units={name: "Count"},
    )


__all__ = ["emit_handoff_metric"]
