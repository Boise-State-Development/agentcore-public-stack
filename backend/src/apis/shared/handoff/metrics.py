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
    surface: Optional[str] = None,
    count: int = 1,
) -> None:
    """One count of ``name`` (``HandoffCreated``, ``HandoffCancelled`` …), with ``kind`` as a property.

    ``detail`` rides as a property too: the interrupt kind on ``HandoffNeedsInput``, the reason on
    ``HandoffStopped``, the interrupt kind on the ``RelayedPrompt*`` counts. ``surface`` is where a
    relayed prompt was answered (``source`` / ``target``). ``count`` is the value for a metric
    that counts things (``HandoffFiles``, ``DeliveryFiles``). Properties, not dimensions, like
    every other caller of ``emit_emf_metrics``.
    """
    properties = {"handoffKind": kind, "sessionId": session_id, "handoffDetail": detail}
    if surface is not None:
        properties["relaySurface"] = surface
    emit_emf_metrics(
        _NAMESPACE,
        metrics={name: count},
        properties=properties,
        units={name: "Count"},
    )


def emit_workflow_run_metric(
    name: str,
    *,
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    step_count: Optional[int] = None,
    detail: Optional[str] = None,
    count: int = 1,
) -> None:
    """One count of a workflow run's ``name`` (``docs/specs/workflow-runs.md`` §15).

    ``WorkflowRunCreated`` carries the run's ``stepCount`` and its ``runSource`` (``user`` in step
    1; saved workflows and model proposals come later). ``WorkflowRunApproved`` carries the steps
    left, ``WorkflowRunEdited`` what the approval changed as ``runDetail`` (``request``,
    ``step_removed``, ``file_unticked``; one count each), ``WorkflowRunCancelled`` a run dismissed
    at its sheet, and ``WorkflowRunStopped`` an approved run stopped. Properties, not dimensions.
    Best-effort: never raises.
    """
    properties: dict = {"sessionId": session_id, "runId": run_id, "runSource": "user"}
    if step_count is not None:
        properties["stepCount"] = step_count
    if detail is not None:
        properties["runDetail"] = detail
    emit_emf_metrics(
        _NAMESPACE,
        metrics={name: count},
        properties=properties,
        units={name: "Count"},
    )


__all__ = ["emit_handoff_metric", "emit_workflow_run_metric"]
