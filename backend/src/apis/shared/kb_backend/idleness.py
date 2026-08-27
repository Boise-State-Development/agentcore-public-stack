"""When was this knowledge base last actually needed?

Requirements 22.5, 22.6. Two rules, and both exist because the obvious answer is
wrong in a way that destroys data.

**Idleness is not retrieval (22.5).** A knowledge base is idle when *nothing* has
needed it, and retrieval is only one of the ways it gets needed. An agent can be
invoked hundreds of times a day and retrieve nothing from its own corpus, because
retrieval only fires when the query matches — so a corpus judged by retrieval alone
looks abandoned precisely when its agent is busiest with questions the documents do
not answer. The follow-up spec's eviction pass would then delete the documents
behind a live agent. So idleness is the maximum of the knowledge base's own
``lastRetrievedAt`` and the ``lastUsedAt`` of any agent bound to it.

While this phase holds ``App_KB_Id == assistant_id`` there is exactly one bound
agent and it is the assistant itself, so "any bound agent" is one ``METADATA``
read. That is deliberately written as a maximum over a set rather than a single
lookup: F4 makes the set larger, and a maximum over one element is the same code.

**Never write a timestamp per retrieval (22.6).** Retrieval is the hot path. The
write is therefore conditional on the stored value being older than a throttle
window, so at most one write lands per window no matter how many turns race — the
same shape as ``assistants.service.bump_last_used_at``, which solved this for
``lastUsedAt`` and is the precedent being followed rather than a second invention.
A conditional write that loses is not a write; it is a rejected update, which is
why calling this on every retrieval is consistent with the requirement.

Nothing here reclaims anything
------------------------------
``reclaim`` is reserved in the migration state enum and never entered in this
phase. This module exists now anyway, because the eviction threshold the follow-up
spec has to choose can only be chosen from historical idleness data, and that data
cannot be backfilled — a timestamp nobody recorded in August is not available in
November.

Feature: managed-kb-migration
Requirements: 22.1, 22.5, 22.6
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: One write per knowledge base per day at most. Chosen to match
#: ``bump_last_used_at``'s default: idleness is measured in days, so a finer
#: resolution buys nothing and costs a write per turn.
THROTTLE_HOURS = 24

#: Attribute on the KB_Record.
LAST_RETRIEVED_ATTR = "lastRetrievedAt"

#: Strong references to detached touch tasks. Without this the only reference is
#: the one ``create_task`` returns, and a dropped task can be collected mid-flight.
_IN_FLIGHT: set = set()


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def throttle_hours() -> int:
    """Resolved at call time, never as a default argument.

    A module constant bound into a signature is captured once at import, so a test
    overriding it silently gets the production value. That cost this feature a
    33-second test that ignored its own override.
    """
    raw = os.environ.get("KB_LAST_RETRIEVED_THROTTLE_HOURS")
    try:
        value = int(raw) if raw else THROTTLE_HOURS
    except ValueError:
        return THROTTLE_HOURS
    return max(value, 1)


def touch_last_retrieved(assistant_id: str, app_kb_id: str) -> bool:
    """Record that this knowledge base served a retrieval. Never raises.

    Returns ``True`` only for the caller whose write actually landed — at most one
    per throttle window. Callers do not need the result; it is returned because a
    boolean that names the winner is what let ``bump_last_used_at`` hang
    resume-on-first-use off the same write, and the reconciler may want the same
    hook later.

    Guarded on ``attribute_exists(SK)`` as well as the freshness floor, so this
    cannot bring a KB_Record into existence. A legacy knowledge base has no record
    and must keep having none: the migration's zero-backfill property is that
    nothing writes to these 1,692 rows until their owner opts in, and a metrics
    side effect that created rows would break it while looking harmless.
    """
    if not os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME"):
        return False

    try:
        from datetime import timedelta

        from apis.shared.kb_backend.records import kb_pk, kb_sk

        now = _now()
        floor = _iso(now - timedelta(hours=throttle_hours()))
        _table().update_item(
            Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
            UpdateExpression=f"SET {LAST_RETRIEVED_ATTR} = :now",
            ConditionExpression=(
                f"attribute_exists(SK) AND (attribute_not_exists({LAST_RETRIEVED_ATTR}) "
                f"OR {LAST_RETRIEVED_ATTR} < :floor)"
            ),
            ExpressionAttributeValues={":now": _iso(now), ":floor": floor},
        )
        return True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # Fresh enough, or there is no KB_Record. Both are ordinary.
            return False
        logger.warning(f"could not record lastRetrievedAt for kb {app_kb_id}: {exc}")
        return False


def schedule_activity_touch(assistant_id: str, app_kb_id: str) -> None:
    """Run :func:`touch_last_retrieved` off the request path. Never raises.

    Retrieval must not wait for a bookkeeping write, nor for its rejection — and
    rejection is the *common* case, since at most one write per throttle window
    lands. The write goes to a thread because boto3 is synchronous and blocking the
    event loop would make every other in-flight turn pay for it.

    Fire-and-forget with a strong reference held until completion, the same pattern
    and the same reason as the dual-read pilot: ``create_task`` returns the only
    reference, and dropping it lets the task be collected mid-flight, which shows up
    as timestamps that silently stop being recorded under load.

    Falls back to doing nothing at all when there is no running loop. A missing
    idleness sample is a gap in a baseline metric; an exception here would be a
    failed retrieval.
    """
    import asyncio

    async def _touch() -> None:
        try:
            await asyncio.to_thread(touch_last_retrieved, assistant_id, app_kb_id)
        except Exception as exc:  # noqa: BLE001 - observability only
            logger.debug(f"lastRetrievedAt touch skipped for {app_kb_id}: {exc}")

    try:
        task = asyncio.create_task(_touch())
    except RuntimeError:
        return

    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)


def bound_agent_ids(assistant_id: str, record: Optional[Mapping[str, Any]] = None) -> list:
    """The agents bound to this knowledge base.

    One, this phase, and it is the assistant itself (Requirement 6.5). Written as a
    list so that the caller below is a maximum over a set today and stays one when
    F4 makes the set bigger — the alternative is a single lookup that has to be
    rewritten, in the module whose whole point is not to under-report activity.
    """
    return [assistant_id]


def agent_last_used_at(assistant_id: str) -> Optional[str]:
    """The assistant's ``lastUsedAt``, read from its ``METADATA`` row.

    Raw table access rather than the assistants service, for this package's usual
    reason: importing ``apis.shared.assistants`` pulls the embeddings stack into a
    size-constrained Lambda image.
    """
    try:
        response = _table().get_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})
    except Exception as exc:
        logger.warning(f"could not read lastUsedAt for assistant {assistant_id}: {exc}")
        return None
    item = response.get("Item") or {}
    for key in ("lastUsedAt", "updatedAt", "createdAt"):
        value = item.get(key)
        if value:
            return str(value)
    return None


def last_activity_at(
    assistant_id: str,
    record: Optional[Mapping[str, Any]] = None,
    agent_timestamps: Optional[Iterable[Optional[str]]] = None,
) -> Optional[str]:
    """The most recent sign of life: retrieval **or** agent use (Requirement 22.5).

    ``agent_timestamps`` lets a caller sweeping many knowledge bases supply values
    it has already read instead of paying a ``get_item`` per knowledge base. When
    omitted, the bound agents are read here.

    ``None`` means nothing is known — no retrieval recorded and no agent timestamp.
    That is **not** the same as "idle since the beginning of time", and callers must
    not treat it as such: it is what a knowledge base provisioned an hour ago looks
    like. :func:`idle_days` returns ``None`` for it rather than a large number.
    """
    candidates = [str((record or {}).get(LAST_RETRIEVED_ATTR) or "") or None]

    if agent_timestamps is None:
        candidates.extend(
            agent_last_used_at(agent_id)
            for agent_id in bound_agent_ids(assistant_id, record)
        )
    else:
        candidates.extend(agent_timestamps)

    known = [value for value in candidates if value]
    if not known:
        return None
    # ISO-8601 UTC strings compare correctly lexicographically, which is why every
    # timestamp in this feature is written in that exact form.
    return max(known)


def idle_days(
    assistant_id: str,
    record: Optional[Mapping[str, Any]] = None,
    agent_timestamps: Optional[Iterable[Optional[str]]] = None,
    now: Optional[str] = None,
) -> Optional[float]:
    """Days since the last sign of life, or ``None`` if nothing is known.

    ``None`` rather than a default is the whole point: a knowledge base with no
    recorded activity is unmeasured, not maximally idle, and a metric that reported
    "very idle" for every freshly provisioned corpus would be exactly the training
    signal that makes operators stop reading it.
    """
    from datetime import datetime, timezone

    latest = last_activity_at(assistant_id, record, agent_timestamps)
    if not latest:
        return None

    try:
        parsed = datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        except ValueError:
            logger.warning(f"unparseable activity timestamp {latest!r}; treating as unknown")
            return None

    reference = (
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now
        else _now()
    )
    return max((reference - parsed).total_seconds() / 86400.0, 0.0)


__all__ = [
    "LAST_RETRIEVED_ATTR",
    "THROTTLE_HOURS",
    "agent_last_used_at",
    "bound_agent_ids",
    "idle_days",
    "last_activity_at",
    "schedule_activity_touch",
    "throttle_hours",
    "touch_last_retrieved",
]
