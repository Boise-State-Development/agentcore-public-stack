"""Cost rows for the task-model side calls: titles, tool-batch summaries, compaction summaries.

These three calls run on the admin's Task models choice (``apis.shared.models.task_models``),
outside the chat turn. They were left unmetered while they ran on Nova Micro and Nova 2 Lite.
An admin can now put Sonnet or Opus there, so each call writes one ``C#`` row like any other
model call. That row counts against the user's quota, the session's ``totalCost`` and the
release ledger (under ``callKind.<kind>``, never as a turn). See
``docs/specs/admin-model-assignments.md`` §4.6.

**Never before the first token, never in the prompt.** The row is recorded by a
fire-and-forget task scheduled after the side call's ``converse`` returns
(``schedule_side_call_cost``). Nothing awaits it, so a summary task cancelled at the end of
the stream still leaves its row. Nothing it does reaches the conversation.

**Off the event loop.** ``store_message_metadata`` issues its DynamoDB calls synchronously.
A tool summary lands while the turn is still streaming, on every tool batch, so the whole
write (the row, the session aggregate, the quota summary, and the system and ledger rollups
it spawns) runs in a worker thread on an event loop of its own (``_store_in_worker``).

**Kept out of the prompt-cache classifier.** A side call shares no prefix with the turn,
so its row is not classified and is never a chat call's predecessor
(``ledger_rollups.PREFIX_UNRELATED_CALL_KINDS``, read by ``_derive_cache_observability``). Otherwise a
Nova Micro summary row between two chat calls would mark the next one ``modelSwitched`` and
hide its waste as explained.

**An uncataloged default writes a row without a cost, and fires no alarm.** Nova Micro and
Nova 2 Lite, the code defaults, may have no catalog row (dev has none). The row still
carries the tokens, marked ``unpricedDefault`` so the release ledger leaves it out of
``unmeteredCalls``, and adding the row later prices every call from then on. An assigned
model always has a row, so a missing price there is a real gap and fires
``UnmeteredModelCall``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Set

from apis.shared.sessions.ledger_rollups import PREFIX_UNRELATED_CALL_KINDS, SIDE_CALL_KINDS  # noqa: F401

logger = logging.getLogger(__name__)

#: ``callKind`` on a title call's cost row.
SESSION_TITLE = "session_title"
#: ``callKind`` on a tool-batch summary call's cost row.
TOOL_SUMMARY = "tool_summary"
#: ``callKind`` on a compaction summary call's cost row (compression or extraction).
COMPACTION_SUMMARY = "compaction_summary"

# The kind sets (``SIDE_CALL_KINDS``, ``PREFIX_UNRELATED_CALL_KINDS``) live with the
# ledger's call kinds, which every cost-row reader already imports.

#: Set on a row whose model is the slot's default with no catalog row, so it carries no cost.
UNPRICED_DEFAULT = "unpricedDefault"

# Strong references to in-flight cost writes: the loop holds tasks weakly.
_pending: Set[asyncio.Task] = set()


def _usage_of(response: Optional[Mapping[str, Any]]) -> Optional[Dict[str, int]]:
    usage = (response or {}).get("usage")
    if not isinstance(usage, Mapping):
        return None
    out = {
        key: int(usage.get(key) or 0)
        for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    }
    if not any(out.values()):
        return None
    out["totalTokens"] = int(usage.get("totalTokens") or (out["inputTokens"] + out["outputTokens"]))
    return out


def schedule_side_call_cost(
    *,
    call_kind: str,
    session_id: Optional[str],
    user_id: Optional[str],
    choice: Any,
    response: Optional[Mapping[str, Any]],
) -> None:
    """Record one side call's cost row in the background. Never raises, never blocks.

    ``choice`` is the ``TaskModelChoice`` the call ran on and ``response`` its Converse
    response. A call with no session, no user or no reported usage writes nothing.
    """
    try:
        if not session_id or not user_id:
            return
        usage = _usage_of(response)
        if usage is None:
            return
        metrics = (response or {}).get("metrics") or {}
        task = asyncio.get_running_loop().create_task(
            record_side_call_cost(
                call_kind=call_kind,
                session_id=session_id,
                user_id=user_id,
                choice=choice,
                usage=usage,
                latency_ms=metrics.get("latencyMs") if isinstance(metrics, Mapping) else None,
            )
        )
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception:  # noqa: BLE001 - metering must never fail the call it meters
        logger.warning("side call %s: could not schedule its cost row", call_kind, exc_info=True)


async def _pricing_snapshot(choice: Any) -> Optional[Dict[str, Any]]:
    """The rates of the catalog row the call ran on, or ``None`` when it has none."""
    from apis.shared.costs.pricing_config import create_pricing_snapshot, pricing_for_model
    from apis.shared.models.task_models import SOURCE_ASSIGNED

    record = getattr(choice, "record", None)
    if record is not None:
        return {
            **pricing_for_model(record),
            "currency": "USD",
            "snapshotAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    if getattr(choice, "source", None) == SOURCE_ASSIGNED:
        # Resolved while the catalog was unreadable: look the row up now.
        return await create_pricing_snapshot(choice.model_id)
    # The default, read from a catalog that has no row for it.
    return None


def _expects_price(choice: Any) -> bool:
    """Whether a missing price is a gap worth an alarm: anything but an uncataloged default."""
    from apis.shared.models.task_models import SOURCE_DEFAULT

    return getattr(choice, "record", None) is not None or getattr(choice, "source", None) != SOURCE_DEFAULT


async def record_side_call_cost(
    *,
    call_kind: str,
    session_id: str,
    user_id: str,
    choice: Any,
    usage: Dict[str, int],
    latency_ms: Optional[Any] = None,
) -> None:
    """Write one side call's ``C#`` row. Never raises."""
    try:
        from apis.shared.costs.calculator import CostCalculator
        from apis.shared.sessions.models import (
            Attribution,
            LatencyMetrics,
            MessageMetadata,
            ModelInfo,
            PricingSnapshot,
            TokenUsage,
        )

        model_id = str(choice.model_id)
        snapshot = None
        try:
            raw = await _pricing_snapshot(choice)
            snapshot = PricingSnapshot.model_validate(raw) if raw else None
        except Exception:  # noqa: BLE001
            logger.warning("side call %s: pricing lookup failed", call_kind, exc_info=True)

        cost = None
        long_context = False
        if snapshot is not None:
            pricing = snapshot.model_dump(by_alias=True)
            try:
                total, breakdown = CostCalculator.calculate_message_cost(usage, pricing)
                cost = {
                    "total": total,
                    "inputCost": breakdown.input_cost,
                    "outputCost": breakdown.output_cost,
                    "cacheReadCost": breakdown.cache_read_cost,
                    "cacheWriteCost": breakdown.cache_write_cost,
                }
                long_context = CostCalculator.is_long_context(usage, pricing)
            except Exception:  # noqa: BLE001
                logger.warning("side call %s: cost calculation failed", call_kind, exc_info=True)

        expects_price = _expects_price(choice)
        if cost is None and expects_price:
            from apis.shared.observability.emf import emit_unmetered_model_call

            logger.warning(
                "Unmetered %s call: model=%s; usage recorded with no cost, not counted against quota",
                call_kind,
                model_id,
            )
            emit_unmetered_model_call(
                model_id,
                "calculation_failed" if snapshot else "no_pricing",
                surface=call_kind,
                session_id=session_id,
            )

        extra: Dict[str, Any] = {"callKind": call_kind}
        if cost is None and not expects_price:
            # Known and accepted, like the missing alarm: the ledger leaves it out of
            # `unmeteredCalls`, whose readout caveat is for a gap nobody has seen.
            extra[UNPRICED_DEFAULT] = True
        if long_context:
            extra["longContext"] = True
        metadata = MessageMetadata(
            latency=LatencyMetrics(end_to_end_latency=int(latency_ms)) if latency_ms else None,
            token_usage=TokenUsage(
                input_tokens=usage["inputTokens"],
                output_tokens=usage["outputTokens"],
                total_tokens=usage["totalTokens"],
                cache_read_input_tokens=usage["cacheReadInputTokens"] or None,
                cache_write_input_tokens=usage["cacheWriteInputTokens"] or None,
            ),
            model_info=ModelInfo(
                model_id=model_id,
                model_name=getattr(getattr(choice, "record", None), "model_name", None) or model_id,
                provider="bedrock",
                pricing_snapshot=snapshot,
            ),
            attribution=Attribution(
                user_id=user_id,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ),
            cost=cost,
            **extra,
        )
        await asyncio.to_thread(_store_in_worker, session_id, user_id, metadata)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - metering must never fail the call it meters
        logger.warning("side call %s: failed to write its cost row", call_kind, exc_info=True)


def _store_in_worker(session_id: str, user_id: str, metadata: Any) -> None:
    """Run ``store_message_metadata`` on a loop of its own, in this worker thread.

    The store spawns the system and ledger rollups as tasks on the running loop, and
    ``asyncio.run`` cancels whatever is left when its coroutine returns. They are awaited
    here first, so the rollups land before the loop closes.
    """
    from apis.shared.sessions.metadata import store_message_metadata

    async def _run() -> None:
        await store_message_metadata(
            session_id=session_id, user_id=user_id, message_id=None, message_metadata=metadata
        )
        current = asyncio.current_task()
        spawned = [task for task in asyncio.all_tasks() if task is not current]
        if spawned:
            await asyncio.gather(*spawned, return_exceptions=True)

    asyncio.run(_run())
