"""Deliveries on the invocation path (``docs/specs/agent-handoffs-and-consults.md`` §4.8, PR-0.4).

A send that carries ``deliveries: [{deliveryId, text?}]`` takes those cards off the composer and into
its user message. Per card, in request order and at most ``DELIVERY_MAX_PER_TURN``:

1. **Attach it, once.** One conditional write moves the row ``pending`` → ``attached`` for this
   owner and this receiving conversation, and returns it. Two tabs sending one card both try, and
   only the write that succeeds carries it. Nothing is read first.
2. **Use the card as sent.** ``text`` present means the user edited it: re-capped here, because the
   SPA can't raise the cap. Absent means the stored text.
3. **Wrap it** in 0a's wrapper, artifact lines included (``render_delivery_block``), so the model
   reads one format for context from another conversation.

The route prepends the blocks to the model message in the row's ``createdAt`` order. The bubble
shows only what the user typed, through the ``original_message`` displayText split, and a reload
renders the cards from the persisted message itself.

**TTFT.** The constructor does no IO. The attaches start as one task at the head of the stream,
beside the agent build, each in its own worker thread; the route awaits them only where it
assembles the message. A send without the field pays nothing.

**Files** (§4.9, PR-0.4a). A card's ``files`` become reference rows in this conversation, written in
the same worker thread right after that card's conditional flip succeeds, so only the request that
attached it writes them and two tabs write one set. A file whose original already lives here (one
handed back to where it came from) gets no row; its line names the original. ``prefetch`` reads the
cards read-only before the lease so the ``document_read`` gate can see a document coming
(``document_hint``).

**Never refused** (§6): an unknown, foreign, already-attached or dismissed id is skipped with a
warning and the turn runs, so a stale tab can't brick a thread. The same goes for the whole field
with ``HANDOFFS_ENABLED`` off, on a resume, on a continuation, and on a preview session. **Nothing
answers for the user** (§9.6 rule 3): a resume never carries a delivery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.feature_flags import handoffs_enabled
from apis.shared.files.linked_files import OriginalRef, line_for, rows_for, write_reference_rows
from apis.shared.handoff.context import context_hash, render_delivery_block
from apis.shared.handoff.delivery import (
    DELIVERY_MAX_PER_TURN,
    Delivery,
    DeliveryFile,
    DeliveryStore,
    cap_delivery_text,
    get_delivery_store,
)
from apis.shared.handoff.metrics import emit_handoff_metric

logger = logging.getLogger(__name__)

#: Bounds the request field, well above ``DELIVERY_MAX_PER_TURN``, so a malformed body is a 422.
DELIVERY_FIELD_MAX_ITEMS = 20
#: Bounds an edited card on the wire, well above the cap it is cut to.
_DELIVERY_TEXT_FIELD_MAX_CHARS = 200_000

# Background writes are fire-and-forget; holding a reference stops the loop collecting them.
_background: Set["asyncio.Task[Any]"] = set()


class DeliveryAttachRequest(BaseModel):
    """One card a send carries. ``text`` only when the user edited it."""

    model_config = ConfigDict(populate_by_name=True)

    delivery_id: str = Field(..., alias="deliveryId", min_length=1, max_length=64)
    text: Optional[str] = Field(None, max_length=_DELIVERY_TEXT_FIELD_MAX_CHARS)


@dataclass(frozen=True)
class AttachedDelivery:
    """A delivery this turn took, and the block the model reads."""

    row: Delivery
    block: str
    edited: bool


@dataclass
class DeliveryTurn:
    """This request's deliveries. Build with ``for_turn``; ``None`` means the send carries none."""

    user_id: str
    session_id: str
    requests: List[DeliveryAttachRequest]
    store: DeliveryStore = field(default_factory=get_delivery_store)
    _attaching: Optional["asyncio.Task[List[AttachedDelivery]]"] = None
    _attach_ms: float = 0.0
    _hint: Optional["asyncio.Task[bool]"] = None

    @classmethod
    def for_turn(
        cls,
        *,
        user_id: str,
        session_id: str,
        deliveries: Optional[Sequence[DeliveryAttachRequest]],
        is_resume: bool,
        is_continuation: bool,
        is_preview: bool = False,
        store: Optional[DeliveryStore] = None,
    ) -> Optional["DeliveryTurn"]:
        """Decide, with no IO, whether this send attaches any cards."""
        if not deliveries:
            return None
        if is_preview:
            logger.warning("deliveries ignored on a preview session")
            return None
        if not handoffs_enabled():
            logger.warning("deliveries ignored: HANDOFFS_ENABLED is off")
            return None
        if is_resume or is_continuation:
            # A resume answers a paused turn; a card must never be part of that answer (§9.6).
            logger.warning("deliveries ignored on a resume or continuation turn")
            return None
        unique = list({d.delivery_id: d for d in deliveries}.values())
        if len(unique) > DELIVERY_MAX_PER_TURN:
            logger.info("deliveries: %d carried, %d attach this turn", len(unique), DELIVERY_MAX_PER_TURN)
        turn = cls(user_id=user_id, session_id=session_id, requests=unique[:DELIVERY_MAX_PER_TURN])
        if store is not None:
            turn.store = store
        return turn

    def prefetch(self) -> None:
        """Read this send's cards, read-only, to classify their files for the gate (§4.9).

        Before the lease and with no write, so a send that then answers 409 has attached nothing.
        """
        if self._hint is not None:
            return
        ids = [r.delivery_id for r in self.requests]

        async def classify() -> bool:
            from apis.shared.files.document_read import is_document_class

            rows = await asyncio.to_thread(self.store.batch_get, self.user_id, ids)
            return any(
                is_document_class(f.mime_type, f.filename)
                for row in rows
                if row.status == "pending" and row.to_session_id == self.session_id
                for f in row.files
            )

        self._hint = asyncio.create_task(classify())

    def document_hint(self) -> Optional["asyncio.Task[bool]"]:
        """Whether a card carries a document ``document_read`` reads; ``None`` before ``prefetch``."""
        return self._hint

    def start(self) -> None:
        """Begin the attaches. Call once, at the head of the stream, beside the agent build."""
        if self._attaching is None:
            self._attaching = asyncio.create_task(self._attach_all())

    async def blocks(self) -> List[str]:
        """The wrapped cards to prepend, oldest first, or ``[]``. Never raises.

        Logs one ``delivery_attach`` line per send that carries cards: ``waitMs`` is how long the
        turn blocked here for the writes, which is this feature's whole cost before the first
        token (0 when they finished behind the agent build); ``attachMs`` is the writes' own time.
        """
        self.start()
        waited_from = time.perf_counter()
        try:
            attached = await self._attaching  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - a card that can't attach must not cost the turn
            logger.warning("Delivery attach failed; the turn runs without the cards", exc_info=True)
            return []
        wait_ms = (time.perf_counter() - waited_from) * 1000
        logger.info(
            "delivery_attach %s",
            json.dumps(
                {
                    "cards": len(self.requests),
                    "attached": len(attached),
                    "waitMs": round(wait_ms, 1),
                    "attachMs": round(self._attach_ms, 1),
                }
            ),
        )
        return [a.block for a in attached if a.block]

    async def _attach_all(self) -> List[AttachedDelivery]:
        began = time.perf_counter()
        results = await asyncio.gather(
            *(asyncio.to_thread(self._attach_one, request) for request in self.requests),
            return_exceptions=True,
        )
        self._attach_ms = (time.perf_counter() - began) * 1000
        attached: List[AttachedDelivery] = []
        for request, result in zip(self.requests, results):
            if isinstance(result, BaseException):
                logger.warning("delivery %s: attach failed", request.delivery_id, exc_info=result)
            elif result is not None:
                attached.append(result)
        attached.sort(key=lambda a: a.row.created_at)
        for item in attached:
            self._after_attach(item)
        return attached

    def _attach_one(self, request: DeliveryAttachRequest) -> Optional[AttachedDelivery]:
        row = self.store.attach(self.user_id, request.delivery_id, self.session_id)
        if row is None:
            logger.warning("delivery %s ignored: not a pending card on this conversation", request.delivery_id)
            return None
        text = row.text
        edited = False
        if request.text is not None:
            sent = cap_delivery_text(request.text)
            edited = (context_hash(sent) if sent else "") != row.text_hash
            text = sent
        lines = self._share_files(row)
        return AttachedDelivery(
            row=row, block=render_delivery_block(text, row.from_title, row.artifacts, lines), edited=edited
        )

    def _share_files(self, row: Delivery) -> list:
        """The card's files as reference rows here, and their lines. Runs only after this request
        won the card's flip. A failed write drops the lines: the model is never told about a file
        it can't open."""
        if not row.files:
            return []
        originals = [_original(f) for f in row.files]
        rows = rows_for(
            originals, user_id=self.user_id, receiving_session_id=self.session_id, link_id=row.delivery_id
        )
        try:
            write_reference_rows(rows)
        except Exception:  # noqa: BLE001 - the card's text still goes
            logger.warning("delivery %s: writing its files failed; sent without them", row.delivery_id, exc_info=True)
            return []
        return [line_for(o, receiving_session_id=self.session_id, link_id=row.delivery_id) for o in originals]

    def _after_attach(self, item: AttachedDelivery) -> None:
        """Counts, and the ``edited`` flag on the row, in the background: nothing here waits."""
        emit_handoff_metric("DeliveryAttached", "send_back", session_id=self.session_id, detail=item.row.origin)
        if item.edited:
            emit_handoff_metric("DeliveryEdited", "send_back", session_id=self.session_id, detail=item.row.origin)
            task = asyncio.ensure_future(
                asyncio.to_thread(self.store.mark_edited, self.user_id, item.row.delivery_id)
            )
            _background.add(task)
            task.add_done_callback(_mark_edited_done)


def _original(f: DeliveryFile) -> OriginalRef:
    return OriginalRef(
        upload_id=f.upload_id,
        session_id=f.session_id,
        from_title=f.from_title,
        filename=f.filename,
        mime_type=f.mime_type,
        size_bytes=f.size_bytes,
        s3_key=f.s3_key,
        s3_bucket=f.s3_bucket,
        ttl=f.ttl,
    )


def _mark_edited_done(task: "asyncio.Task[Any]") -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("delivery: recording an edit failed", exc_info=task.exception())


def join_delivery_blocks(blocks: Sequence[str], message: str) -> str:
    """The user message with the cards before it, each followed by a blank line."""
    head = "\n\n".join(b for b in blocks if b)
    if not head:
        return message
    return f"{head}\n\n{message}" if message.strip() else head


__all__ = [
    "AttachedDelivery",
    "DELIVERY_FIELD_MAX_ITEMS",
    "DeliveryAttachRequest",
    "DeliveryTurn",
    "join_delivery_blocks",
]
