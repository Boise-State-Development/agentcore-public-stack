"""A handoff on the invocation path (``docs/specs/agent-handoffs-and-consults.md`` §4.2–§4.3, PR-0.2).

Two jobs, both owned by one per-request ``HandoffTurn``:

**The target's first turn takes the handoff.** The request carries ``handoff_context: {handoffId,
text}``. The server checks the ``HANDOFF#`` row is the caller's, still ``open`` and for this Agent,
re-applies the excerpt cap to ``text`` (the SPA can't raise it), accepts the row exactly once
(``HandoffStore.accept``), and returns the wrapped block the route prepends to the model message.
The persisted bubble shows only what the user typed, through the existing ``original_message``
displayText split. The target's ``META`` row gets ``spawnedFrom`` afterwards, in the background.

**Every turn of the target keeps the source's row current** until the first ``done``. The route
feeds each SSE frame to ``observe``; after the stream ends, ``schedule_status_write`` turns what was
seen into ``needs_input`` (with ``waitingOn``), ``done`` (with the latest tool summary as
``statusLine``) or ``stopped`` (with a reason), and writes it in the background. A later turn finds
its handoff from ``spawnedFrom`` on the B1 snapshot, so it costs no read.

**A state change that needs the user goes to their inbox too** (§4.3, PR-0.3b): one
``handoff_needs_input`` / ``handoff_done`` / ``handoff_stopped`` entry, written in the same background
step right after the row moved, so a tab closed mid-turn still leaves a ``handoff_stopped`` for the
next visit. A Stop the user pressed writes none.

**Nothing here may add time before the first token** (CLAUDE.md, TTFT budget). The constructor does
no IO. Acceptance (a read and a conditional write) starts as a task at the head of the stream, so
it runs in a worker thread while the agent is built. A handoff's target is always a brand-new
session, so its build is a cache miss and takes far longer than the two round trips. The route
awaits it only where it assembles the message. Everything else happens after the stream has ended.

**Files by reference** (§4.9, PR-0.4a). ``handoff_context.fileIds`` names source files the user
left ticked on the review sheet. ``prefetch`` reads the ``HANDOFF#`` row and those file rows
together, read-only, before the lease, so the ``document_read`` gate can be told (``document_hint``)
that a document is coming without a query of its own. Acceptance then uses that read, and right
after its conditional write writes one reference row per file in one ``BatchWriteItem`` and adds a
``<file>`` line per file to the block. A file that isn't the caller's, isn't ``READY`` or isn't in
the source conversation is dropped with a warning. A send without files prefetches nothing.

With ``HANDOFFS_ENABLED`` off, a ``handoff_context`` on the request is ignored with a warning, never
refused (§6): a stale SPA must never brick a thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.files.file_kinds import KIND_DOCUMENT, FileKindHints, file_kinds
from apis.shared.feature_flags import handoffs_enabled
from apis.shared.files.linked_files import (
    HANDOFF_MAX_FILES,
    OriginalRef,
    line_for,
    linkable_refusal,
    original_of,
    rows_for,
    write_reference_rows,
)
from apis.shared.handoff.context import normalize_context_text, context_hash, wrap_handoff_context
from apis.shared.handoff.excerpt import CHARS_PER_TOKEN, HANDOFF_EXCERPT_MAX_TOKENS, clip_middle
from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.handoff.models import Handoff, HandoffStatus, WaitingOn
from apis.shared.handoff.notify import notify_handoff_state
from apis.shared.handoff.store import HandoffStore, get_handoff_store

logger = logging.getLogger(__name__)

#: The server-side cap on a context card as sent, edited or not. The same budget the excerpt is
#: rendered to, so an unedited card is never clipped and an edited one can't grow past it.
HANDOFF_CONTEXT_MAX_CHARS = HANDOFF_EXCERPT_MAX_TOKENS * CHARS_PER_TOKEN

#: Bounds the request field itself, well above the cap, so an oversized body is a 422 rather than
#: a multi-megabyte string carried into the turn.
_HANDOFF_CONTEXT_FIELD_MAX_CHARS = 200_000

#: Interrupt events that pause a turn for the user, and how each names what it waits on.
_WAITING_EVENTS = {
    "oauth_required": "oauth_consent",
    "tool_approval_required": "tool_approval",
    "user_question_required": "user_question",
    "browser_login_required": "browser_login",
}

#: The only events the observer reads. Anything else (the text deltas above all) is skipped
#: before its JSON is parsed.
_OBSERVED_EVENTS = frozenset({"tool_group_summary", "stream_error", "message_stop", "done", *_WAITING_EVENTS})

# Background writes are fire-and-forget; holding a reference stops the loop collecting them.
_background: Set["asyncio.Task[Any]"] = set()


class HandoffContextRequest(BaseModel):
    """``handoff_context`` on ``/invocations``: the handoff being sent and its card as sent.

    ``text`` absent or blank means the user removed the card; the handoff is still sent.
    """

    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId", min_length=1, max_length=64)
    text: Optional[str] = Field(None, max_length=_HANDOFF_CONTEXT_FIELD_MAX_CHARS)
    #: Source files to share by reference (§4.9). The first ``HANDOFF_MAX_FILES`` are used.
    file_ids: List[str] = Field(default_factory=list, alias="fileIds", max_length=20)
    #: Files the review sheet listed and the user left unticked, for ``HandoffFilesUnticked``.
    files_unticked: int = Field(0, alias="filesUnticked", ge=0, le=1000)


def cap_context_text(text: str) -> str:
    """The card as the server will use it: normalized and clipped to ``HANDOFF_CONTEXT_MAX_CHARS``."""
    return clip_middle(normalize_context_text(text), HANDOFF_CONTEXT_MAX_CHARS)


@dataclass(frozen=True)
class TurnOutcome:
    """What a target's turn ended as, for the source's ``HANDOFF#`` row."""

    status: HandoffStatus
    status_line: Optional[str] = None
    waiting_on: Optional[WaitingOn] = None
    stopped_reason: Optional[str] = None


@dataclass
class HandoffTurnObserver:
    """Reads the SSE frames a turn streams and decides its outcome. Pure; no IO.

    A frame the observer can't parse is skipped, never raised: this sits in the stream's path and
    must not be able to break it.
    """

    latest_summary: Optional[str] = None
    waiting_on: Optional[WaitingOn] = None
    errored: bool = False
    user_stopped: bool = False
    saw_done: bool = False

    def observe(self, chunk: str) -> None:
        for event, data in _frames(chunk, _OBSERVED_EVENTS):
            if event == "tool_group_summary":
                summary = data.get("summary")
                if isinstance(summary, str) and summary.strip():
                    self.latest_summary = summary.strip()
            elif event in _WAITING_EVENTS:
                waiting = _waiting_on(event, data)
                if waiting is not None and self.waiting_on is None:
                    self.waiting_on = waiting
            elif event == "stream_error":
                self.errored = True
            elif event == "message_stop":
                reason = data.get("stopReason")
                if reason == "stopped":
                    self.user_stopped = True
                elif reason == "error":
                    self.errored = True
            elif event == "done":
                self.saw_done = True

    def outcome(self, *, disconnected: bool) -> TurnOutcome:
        if disconnected:
            return TurnOutcome("stopped", stopped_reason="connection_lost")
        if self.user_stopped:
            return TurnOutcome("stopped", stopped_reason="user_stopped")
        if self.errored or not self.saw_done:
            return TurnOutcome("stopped", stopped_reason="error")
        if self.waiting_on is not None:
            return TurnOutcome("needs_input", waiting_on=self.waiting_on)
        return TurnOutcome("done", status_line=self.latest_summary)


@dataclass
class HandoffTurn:
    """This request's part in a handoff. Build with ``for_turn``; ``None`` means none."""

    user_id: str
    session_id: str
    agent_id: Optional[str]
    request: Optional[HandoffContextRequest] = None
    #: The inbox's key. ``None`` (a token without an email claim) skips the inbox, never the row.
    user_email: Optional[str] = None
    linked_handoff_id: Optional[str] = None
    store: HandoffStore = field(default_factory=get_handoff_store)
    observer: HandoffTurnObserver = field(default_factory=HandoffTurnObserver)
    _acceptance: Optional["asyncio.Task[Tuple[Optional[Handoff], str]]"] = None
    _prefetch: Optional["asyncio.Task[Tuple[Optional[Handoff], Dict[str, Any]]]"] = None
    _kinds: Optional[FileKindHints] = None
    #: The target is a Shared Project task (its row names a project): no file crosses into it.
    target_is_project_task: bool = False

    @classmethod
    def for_turn(
        cls,
        *,
        user_id: str,
        session_id: str,
        agent_id: Optional[str],
        handoff_context: Optional[HandoffContextRequest],
        session_row: Optional[Dict[str, Any]],
        is_resume: bool,
        is_continuation: bool,
        is_preview: bool = False,
        user_email: Optional[str] = None,
        store: Optional[HandoffStore] = None,
    ) -> Optional["HandoffTurn"]:
        """Decide, with no IO, whether this turn takes a handoff or reports on one.

        A preview session (Agent Designer's test chat) is never a handoff's target: it is not
        persisted, so there would be no conversation to link to.
        """
        if is_preview:
            if handoff_context is not None:
                logger.warning("handoff_context ignored on a preview session")
            return None
        if not handoffs_enabled():
            if handoff_context is not None:
                logger.warning("handoff_context ignored: HANDOFFS_ENABLED is off")
            return None

        linked = _linked_handoff_id(session_row)
        request = handoff_context
        if request is not None and (is_resume or is_continuation):
            logger.warning("handoff_context ignored on a resume or continuation turn")
            request = None
        if request is not None and linked is not None:
            # Already a handoff's target: a second card would be a second, conflicting context.
            logger.warning("handoff_context ignored: this conversation already took a handoff")
            request = None
        if request is None and linked is None:
            return None
        turn = cls(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            request=request,
            linked_handoff_id=linked,
            user_email=user_email or None,
            target_is_project_task=bool(_project_of(session_row)),
        )
        if store is not None:
            turn.store = store
        return turn

    @property
    def handoff_id(self) -> Optional[str]:
        return self.request.handoff_id if self.request is not None else self.linked_handoff_id

    def _file_ids(self) -> List[str]:
        if self.request is None:
            return []
        wanted = list(dict.fromkeys(f for f in self.request.file_ids if f))
        if len(wanted) > HANDOFF_MAX_FILES:
            logger.warning("handoff_context: %d files sent, %d are shared", len(wanted), HANDOFF_MAX_FILES)
        return wanted[:HANDOFF_MAX_FILES]

    def prefetch(self) -> None:
        """Read the handoff row and its files, read-only, in worker threads. Only with files.

        Call before the lease: nothing here can take the handoff, so a request that then answers
        409 has spent nothing. Acceptance reuses the row this reads.
        """
        file_ids = self._file_ids()
        if not file_ids or self._prefetch is not None:
            return
        from apis.shared.files.repository import get_file_upload_repository

        async def read() -> Tuple[Optional[Handoff], Dict[str, Any]]:
            assert self.request is not None
            row, files = await asyncio.gather(
                asyncio.to_thread(self.store.get, self.user_id, self.request.handoff_id),
                asyncio.to_thread(get_file_upload_repository().batch_get_files_sync, self.user_id, file_ids),
            )
            return row, files

        self._prefetch = asyncio.create_task(read())

    def kind_hint(self, kind: str) -> Optional["asyncio.Task[bool]"]:
        """Whether the shared files include ``kind`` (``file_kinds.KIND_*``); ``None`` without files.

        Each tool gate that reads the conversation's files awaits its own kind beside its query:
        the reference rows are written during the stream, after the gates ran (§4.9).
        """
        if self._prefetch is None:
            return None
        if self._kinds is None:

            async def classify() -> FrozenSet[str]:
                row, files = await self._prefetch  # type: ignore[misc]
                if row is None:
                    return frozenset()
                return file_kinds((o.mime_type, o.filename) for o in self._shareable(row, files))

            self._kinds = FileKindHints(asyncio.ensure_future(classify()))
        return self._kinds.hint(kind)

    def document_hint(self) -> Optional["asyncio.Task[bool]"]:
        """Whether the shared files include one ``document_read`` reads; ``None`` without files."""
        return self.kind_hint(KIND_DOCUMENT)

    def _shareable(self, row: Handoff, files: Dict[str, Any]) -> List[OriginalRef]:
        """The source files that may cross, as originals, in the order the user listed them.

        None into or out of a Shared Project task (§4.9): other members can read that conversation.
        """
        if row.source_project_id or self.target_is_project_task:
            if self._file_ids():
                logger.warning("handoff_context: files not shared (project_task)")
            return []
        out: List[OriginalRef] = []
        for upload_id in self._file_ids():
            meta = files.get(upload_id)
            reason = linkable_refusal(meta, user_id=self.user_id, sender_session_id=row.session_id)
            if reason is not None:
                logger.warning("handoff_context: a file was not shared (%s)", reason)
                continue
            out.append(original_of(meta, holder_title=row.source_title))
        return out

    def start(self) -> None:
        """Begin acceptance in a worker thread. Call once, at the head of the stream."""
        if self.request is None or self._acceptance is not None:
            return
        if self._prefetch is None:
            self._acceptance = asyncio.create_task(asyncio.to_thread(self._accept))
            return

        async def accept_after_prefetch() -> Tuple[Optional[Handoff], str]:
            row, files = await self._prefetch  # type: ignore[misc]
            return await asyncio.to_thread(self._accept, (row, files))

        self._acceptance = asyncio.create_task(accept_after_prefetch())

    async def context_block(self) -> str:
        """The wrapped context to prepend to the model message, or ``""``.

        Awaits acceptance. Empty when there is nothing to carry (no request, the card was removed,
        or the handoff was refused), so the turn runs as an ordinary first turn rather than failing.
        """
        if self.request is None:
            return ""
        self.start()
        try:
            accepted, block = await self._acceptance  # type: ignore[misc]
        except Exception:
            logger.warning("Handoff acceptance failed; the turn runs without context", exc_info=True)
            return ""
        if accepted is not None:
            self._run_in_background(self._link_target, accepted)
        return block

    def observe(self, chunk: str) -> None:
        try:
            self.observer.observe(chunk)
        except Exception:  # noqa: BLE001 - must never break the stream it watches
            logger.debug("Handoff observer skipped a chunk", exc_info=True)

    def schedule_status_write(self, *, disconnected: bool) -> None:
        """After the stream: write what this turn ended as onto the source's row, in the background."""
        outcome = self.observer.outcome(disconnected=disconnected)
        task = asyncio.ensure_future(self._write_status(outcome))
        _background.add(task)
        task.add_done_callback(_background.discard)

    def _accept(
        self, prefetched: Optional[Tuple[Optional[Handoff], Dict[str, Any]]] = None
    ) -> Tuple[Optional[Handoff], str]:
        request = self.request
        assert request is not None
        row, files = prefetched if prefetched is not None else (self.store.get(self.user_id, request.handoff_id), {})
        if row is None:
            logger.warning("handoff_context ignored: no such handoff for this user")
            return None, ""
        if row.agent_id != self.agent_id:
            logger.warning("handoff_context ignored: the handoff is for a different Agent")
            return None, ""
        text = cap_context_text(request.text or "")
        removed = not text
        # The card the SPA offered: the brief when one was written for this handoff, else the excerpt.
        offered_hash = row.brief_hash or row.excerpt_hash
        edited = not removed and context_hash(text) != offered_hash
        accepted = self.store.accept(
            self.user_id,
            request.handoff_id,
            target_session_id=self.session_id,
            context_edited=edited,
            context_removed=removed,
        )
        if accepted is None:
            logger.warning("handoff_context ignored: the handoff was already sent or was dismissed")
            return None, ""
        emit_handoff_metric("HandoffSent", accepted.kind, session_id=accepted.session_id)
        if edited:
            emit_handoff_metric("HandoffContextEdited", accepted.kind, session_id=accepted.session_id)
        if removed:
            emit_handoff_metric("HandoffContextRemoved", accepted.kind, session_id=accepted.session_id)
        lines = self._share_files(accepted, files) if prefetched is not None else []
        if request.files_unticked:
            emit_handoff_metric(
                "HandoffFilesUnticked", accepted.kind, session_id=accepted.session_id, count=request.files_unticked
            )
        return accepted, wrap_handoff_context(text, accepted.source_title, lines)

    def _share_files(self, accepted: Handoff, files: Dict[str, Any]) -> list:
        """Write the reference rows for the shared files and return their lines (§4.9).

        Only after the conditional accept succeeded, so a refused handoff writes nothing. A failed
        write drops the lines too: the model is never told about a file it can't open.
        """
        originals = self._shareable(accepted, files)
        if not originals:
            return []
        rows = rows_for(
            originals, user_id=self.user_id, receiving_session_id=self.session_id, link_id=accepted.handoff_id
        )
        began = time.perf_counter()
        try:
            write_reference_rows(rows)
        except Exception:  # noqa: BLE001 - the handoff still runs, without its files
            logger.warning("handoff: writing the shared files failed; sent without them", exc_info=True)
            return []
        logger.info(
            "handoff_files %s",
            json.dumps({"files": len(originals), "rows": len(rows), "writeMs": round((time.perf_counter() - began) * 1000, 1)}),
        )
        emit_handoff_metric("HandoffFiles", accepted.kind, session_id=accepted.session_id, count=len(originals))
        return [
            line_for(o, receiving_session_id=self.session_id, link_id=accepted.handoff_id) for o in originals
        ]

    def _link_target(self, accepted: Handoff) -> None:
        from apis.shared.sessions.metadata import set_session_spawned_from

        set_session_spawned_from(self.session_id, self.user_id, accepted.session_id, accepted.handoff_id)

    async def _write_status(self, outcome: TurnOutcome) -> None:
        handoff_id = self.handoff_id
        if handoff_id is None:
            return
        if self._acceptance is not None:
            # The first turn: report only on a handoff this turn actually took.
            try:
                accepted, _ = await self._acceptance
            except Exception:  # noqa: BLE001 - logged where it was awaited
                return
            if accepted is None:
                return
        try:
            await asyncio.to_thread(self._transition, handoff_id, outcome)
        except Exception:  # noqa: BLE001 - a lost status write costs only the row's freshness
            logger.warning("Handoff status write failed", exc_info=True)

    def _transition(self, handoff_id: str, outcome: TurnOutcome) -> None:
        fields: Dict[str, Any] = {}
        if outcome.status_line:
            fields["statusLine"] = outcome.status_line
        if outcome.waiting_on is not None:
            fields["waitingOn"] = outcome.waiting_on.model_dump()
        if outcome.stopped_reason:
            fields["stoppedReason"] = outcome.stopped_reason
        updated = self.store.transition(self.user_id, handoff_id, outcome.status, **fields)
        if updated is not None:
            metric = {
                "done": "HandoffCompleted",
                "needs_input": "HandoffNeedsInput",
                "stopped": "HandoffStopped",
            }[outcome.status]
            detail = outcome.waiting_on.kind if outcome.waiting_on else outcome.stopped_reason
            emit_handoff_metric(metric, updated.kind, session_id=updated.session_id, detail=detail)
            notify_handoff_state(
                updated,
                outcome.status,
                recipient_email=self.user_email,
                target_session_id=self.session_id,
                stopped_reason=outcome.stopped_reason,
            )
        if updated is None or outcome.status == "done":
            # Done, or a row that can no longer move (frozen, or gone with its source): stop
            # following this conversation's turns.
            from apis.shared.sessions.metadata import settle_session_spawned_from

            settle_session_spawned_from(self.session_id, self.user_id)

    def _run_in_background(self, fn: Any, *args: Any) -> None:
        async def run() -> None:
            try:
                await asyncio.to_thread(fn, *args)
            except Exception:  # noqa: BLE001 - background bookkeeping
                logger.warning("Handoff background write failed", exc_info=True)

        task = asyncio.ensure_future(run())
        _background.add(task)
        task.add_done_callback(_background.discard)


def _project_of(session_row: Optional[Dict[str, Any]]) -> Optional[str]:
    from apis.shared.handoff.send_back import project_of

    return project_of(session_row)


def _linked_handoff_id(session_row: Optional[Dict[str, Any]]) -> Optional[str]:
    spawned = (session_row or {}).get("spawnedFrom")
    if not isinstance(spawned, dict) or spawned.get("settled"):
        return None
    handoff_id = spawned.get("handoffId")
    return handoff_id if isinstance(handoff_id, str) and handoff_id else None


def _waiting_on(event: str, data: Dict[str, Any]) -> Optional[WaitingOn]:
    kind = _WAITING_EVENTS[event]
    if kind == "oauth_consent":
        # A pre-flight consent (no interruptId) paused nothing; the turn went on without the tool.
        if not data.get("interruptId"):
            return None
        return WaitingOn(kind=kind, name=str(data.get("providerId") or ""))
    if kind == "tool_approval":
        return WaitingOn(kind=kind, name=str(data.get("toolName") or ""))
    if kind == "user_question":
        questions = data.get("questions") or []
        header = questions[0].get("header") if questions and isinstance(questions[0], dict) else ""
        return WaitingOn(kind=kind, name=str(header or ""))
    return WaitingOn(kind=kind)


def _frames(chunk: str, wanted: frozenset) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """``(event, data)`` for each SSE frame in a chunk whose event is in ``wanted``.

    Frames without JSON data get ``{}``. An unwanted frame is skipped before its data is parsed.
    """
    for frame in chunk.split("\n\n"):
        if not frame.startswith("event:"):
            continue
        head, _, rest = frame.partition("\n")
        event = head[len("event:") :].strip()
        if event not in wanted:
            continue
        data_lines = [line[len("data:") :].strip() for line in rest.split("\n") if line.startswith("data:")]
        data: Dict[str, Any] = {}
        if data_lines:
            try:
                parsed = json.loads("\n".join(data_lines))
                if isinstance(parsed, dict):
                    data = parsed
            except ValueError:
                pass
        yield event, data


__all__ = [
    "HANDOFF_CONTEXT_MAX_CHARS",
    "HandoffContextRequest",
    "HandoffTurn",
    "HandoffTurnObserver",
    "TurnOutcome",
    "cap_context_text",
]
