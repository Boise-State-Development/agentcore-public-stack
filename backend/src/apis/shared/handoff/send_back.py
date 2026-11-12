"""Making a delivery, and the return excerpt a send-back offers (spec §4.3, §4.8, PR-0.4).

**``create_delivery`` is the only way a ``DELIVERY#`` row is written.** Phase 0's two send-back paths
call it; Phase 2's ``send_to_conversation`` will too. It refuses, with a ``DeliveryRefused`` naming
why, unless:

- both conversations are the caller's, read on ``(session, user)``
- they are linked: one was spawned from the other (§9.2's reach rule; Phase 0 only sends back)
- neither is a Shared Project task, whose other members can read a conversation none of them sent
  text into (§4.8)
- every artifact was made by the caller **in the sending conversation**. Artifacts are keyed by
  user, not by conversation, so without the second half a delivery could carry any artifact the user
  owns from an unrelated conversation (§4.8, "Only the sender's own artifacts")

**The return excerpt** is one assistant message's text, bounded at ``SEND_BACK_MAX_TOKENS`` and cut
at a paragraph break (``cap_delivery_text``). Tool calls and reasoning are left out: what comes back
is the answer, not the work.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from apis.shared.handoff.context import context_hash
from apis.shared.handoff.delivery import (
    DELIVERY_MAX_ARTIFACTS,
    Delivery,
    DeliveryArtifact,
    DeliveryOrigin,
    DeliveryStore,
    cap_delivery_text,
    get_delivery_store,
)
from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)


class DeliveryRefused(Exception):
    """``create_delivery`` would not write the row. ``reason`` is stable, for the route and metrics."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LinkedPair:
    """The two ``META`` rows a delivery moves between, both the caller's."""

    sender: Dict[str, Any]
    receiver: Dict[str, Any]


def render_return_excerpt(message: Mapping[str, Any]) -> str:
    """One assistant message's answer, as a send-back's card: its text, capped. ``""`` if it has none."""
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    texts = [
        str(block["text"]).strip()
        for block in blocks
        if isinstance(block, dict)
        and isinstance(block.get("text"), str)
        # ``MessageContent`` names its kind; a bare Converse block is text only when it is nothing else.
        and block.get("type", "text") == "text"
        and not any(k in block for k in ("toolUse", "toolResult", "reasoningContent", "document", "image"))
    ]
    return cap_delivery_text("\n\n".join(t for t in texts if t))


async def load_linked_pair(user_id: str, sender_id: str, receiver_id: str) -> LinkedPair:
    """Both conversations' ``META`` rows, or ``DeliveryRefused``. Two reads, issued together."""
    from apis.shared.sessions.metadata import load_session_meta

    if sender_id == receiver_id:
        raise DeliveryRefused("same_conversation")
    sender, receiver = await asyncio.gather(
        load_session_meta(sender_id, user_id), load_session_meta(receiver_id, user_id)
    )
    if sender.row is None or receiver.row is None:
        raise DeliveryRefused("not_found")
    if sender.row.get("projectId") or receiver.row.get("projectId"):
        raise DeliveryRefused("project_task")
    if not (_spawned_from(sender.row) == receiver_id or _spawned_from(receiver.row) == sender_id):
        raise DeliveryRefused("not_linked")
    return LinkedPair(sender=sender.row, receiver=receiver.row)


def offered_artifacts(user_id: str, session_id: str, message_index: int) -> List[DeliveryArtifact]:
    """The artifacts a message produced, at HEAD, newest first: what a send-back of it offers."""
    heads = _artifact_heads(user_id, session_id)
    return [_artifact(h) for h in heads if h.get("produced_by_message_index") == message_index][
        :DELIVERY_MAX_ARTIFACTS
    ]


async def create_delivery(
    *,
    user_id: str,
    from_session_id: str,
    to_session_id: str,
    text: str,
    origin: DeliveryOrigin = "user",
    handoff_id: Optional[str] = None,
    artifact_ids: Sequence[str] = (),
    pair: Optional[LinkedPair] = None,
    store: Optional[DeliveryStore] = None,
) -> Delivery:
    """Write a ``pending`` delivery into ``to_session_id``, or raise ``DeliveryRefused``.

    ``pair`` skips the session reads when the caller already made them with ``load_linked_pair``.
    """
    row = await prepare_delivery(
        user_id=user_id,
        from_session_id=from_session_id,
        to_session_id=to_session_id,
        text=text,
        origin=origin,
        handoff_id=handoff_id,
        artifact_ids=artifact_ids,
        pair=pair,
    )
    return await write_delivery(row, store=store)


async def write_delivery(row: Delivery, *, store: Optional[DeliveryStore] = None) -> Delivery:
    """Write a row ``prepare_delivery`` built. The checks are already done."""
    store = store or get_delivery_store()
    await asyncio.to_thread(store.create, row)
    emit_handoff_metric("DeliveryCreated", "send_back", session_id=row.to_session_id, detail=row.origin)
    if row.artifacts:
        emit_handoff_metric(
            "DeliveryArtifacts", "send_back", session_id=row.to_session_id, detail=str(len(row.artifacts))
        )
    return row


async def prepare_delivery(
    *,
    user_id: str,
    from_session_id: str,
    to_session_id: str,
    text: str,
    origin: DeliveryOrigin = "user",
    handoff_id: Optional[str] = None,
    artifact_ids: Sequence[str] = (),
    pair: Optional[LinkedPair] = None,
) -> Delivery:
    """Every check ``create_delivery`` makes, and the row it would write, without writing it.

    For a caller that has its own exactly-once step to take between the checks and the write (a
    send-back closes its ``HANDOFF#`` row there), so a refusal never leaves that step taken.
    """
    pair = pair or await load_linked_pair(user_id, from_session_id, to_session_id)

    wanted = list(dict.fromkeys(artifact_ids))
    if len(wanted) > DELIVERY_MAX_ARTIFACTS:
        raise DeliveryRefused("too_many_artifacts")
    artifacts: List[DeliveryArtifact] = []
    if wanted:
        heads = {h["artifact_id"]: h for h in await asyncio.to_thread(_artifact_heads, user_id, from_session_id)}
        missing = [a for a in wanted if a not in heads]
        if missing:
            # Another user's, from another conversation, or gone: one answer for all three.
            raise DeliveryRefused("artifact_not_in_sender")
        artifacts = [_artifact(heads[a]) for a in wanted]

    body = cap_delivery_text(text)
    if not body and not artifacts:
        raise DeliveryRefused("empty")

    now = utc_now_iso()
    return Delivery(
        delivery_id=str(uuid.uuid4()),
        user_id=user_id,
        to_session_id=to_session_id,
        from_session_id=from_session_id,
        from_title=str(pair.sender.get("title") or ""),
        handoff_id=handoff_id,
        origin=origin,
        text=body,
        text_hash=context_hash(body) if body else "",
        artifacts=artifacts,
        status="pending",
        created_at=now,
        updated_at=now,
    )


def _spawned_from(row: Mapping[str, Any]) -> Optional[str]:
    spawned = row.get("spawnedFrom")
    if isinstance(spawned, Mapping):
        source = spawned.get("sessionId")
        return source if isinstance(source, str) else None
    return None


def _artifact_heads(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    from apis.shared.shares.artifacts import artifacts_table, session_artifact_heads

    table = artifacts_table()
    if table is None:
        return []
    return session_artifact_heads(table, user_id=user_id, session_id=session_id)


def _artifact(head: Mapping[str, Any]) -> DeliveryArtifact:
    return DeliveryArtifact(
        artifact_id=str(head["artifact_id"]),
        version=int(head.get("version", 1)),
        title=str(head.get("title", "")),
        content_type=str(head.get("content_type", "")),
    )


__all__ = [
    "DeliveryRefused",
    "LinkedPair",
    "create_delivery",
    "load_linked_pair",
    "offered_artifacts",
    "prepare_delivery",
    "render_return_excerpt",
    "write_delivery",
]
