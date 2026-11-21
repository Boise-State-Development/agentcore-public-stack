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
- every file is the caller's, ``READY``, and a row of the sending conversation (§4.9). A reference
  the sender holds is carried as its original, so the receiver's row is one hop from the bytes and
  a file handed back to where it came from needs no row at all

**A run result** (``docs/specs/workflow-runs.md`` §7, PR-W.2) is one row with a part per step.
``prepare_delivery(parts=…)`` applies every rule above to **each** part, against that part's own
sending conversation, with the receiver read once; there is no second set of checks.

**The return excerpt** is one assistant message's text, bounded at ``SEND_BACK_MAX_TOKENS`` and cut
at a paragraph break (``cap_delivery_text``). Tool calls and reasoning are left out: what comes back
is the answer, not the work.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from apis.shared.handoff.context import context_hash
from apis.shared.files.linked_files import DELIVERY_MAX_FILES, linkable_refusal, original_of
from apis.shared.handoff.delivery import (
    DELIVERY_MAX_ARTIFACTS,
    DELIVERY_MAX_PARTS,
    RUN_RESULT_MAX_FILES,
    Delivery,
    DeliveryArtifact,
    DeliveryFile,
    DeliveryOrigin,
    DeliveryPart,
    DeliveryPartStatus,
    DeliveryStore,
    cap_delivery_text,
    delivery_file_count,
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


@dataclass(frozen=True)
class DeliveryPartRequest:
    """One step's share of a run result, before the checks (``prepare_delivery(parts=…)``).

    ``from_session_id`` is the step's conversation, empty for a step that never started. Only a
    ``done`` part may carry text, artifacts or files.

    A prompt step's part (``prompt=True``, workflow-designer §21.3) has no conversation at all: it
    is text alone, the step's own answer, with no sender to check, no artifacts and no files.
    ``from_title`` is what its block names beside ``agent_name`` (the step, beside the model);
    an Agent step's comes from its conversation's title instead, and this is ignored.
    """

    step_id: str
    agent_name: str
    status: DeliveryPartStatus
    from_session_id: str = ""
    text: str = ""
    artifact_ids: Sequence[str] = ()
    file_ids: Sequence[str] = ()
    prompt: bool = False
    from_title: str = ""


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
    return (await load_linked_senders(user_id, [sender_id], receiver_id))[sender_id]


async def load_linked_senders(user_id: str, sender_ids: Sequence[str], receiver_id: str) -> Dict[str, LinkedPair]:
    """Each sender's pair with the receiver, or ``DeliveryRefused`` if any one fails.

    The receiver is read once and every sender beside it, all issued together. Each sender is held
    to the same rules: the caller's, not a Shared Project task, and linked to the receiver.
    """
    from apis.shared.sessions.metadata import load_session_meta

    senders = list(dict.fromkeys(sender_ids))
    if receiver_id in senders:
        raise DeliveryRefused("same_conversation")
    receiver, *rows = await asyncio.gather(
        load_session_meta(receiver_id, user_id), *(load_session_meta(s, user_id) for s in senders)
    )
    if receiver.row is None or any(r.row is None for r in rows):
        raise DeliveryRefused("not_found")
    if project_of(receiver.row) or any(project_of(r.row) for r in rows):
        raise DeliveryRefused("project_task")
    for sender_id, sender in zip(senders, rows):
        if not (_spawned_from(sender.row) == receiver_id or _spawned_from(receiver.row) == sender_id):
            raise DeliveryRefused("not_linked")
    return {s: LinkedPair(sender=r.row, receiver=receiver.row) for s, r in zip(senders, rows)}


async def load_receiver(user_id: str, receiver_id: str) -> Dict[str, Any]:
    """The receiving conversation's ``META`` row, held to a receiver's rules, or ``DeliveryRefused``.

    For a run result with no sending conversation (only prompt steps' parts): the caller's, and not
    a Shared Project task, exactly as ``load_linked_senders`` holds a receiver.
    """
    from apis.shared.sessions.metadata import load_session_meta

    receiver = await load_session_meta(receiver_id, user_id)
    if receiver.row is None:
        raise DeliveryRefused("not_found")
    if project_of(receiver.row):
        raise DeliveryRefused("project_task")
    return receiver.row


async def answer_message(
    session_id: str,
    user_id: str,
    *,
    message_index: Optional[int] = None,
    at_or_before: Optional[str] = None,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """The assistant message a send-back carries, and its index, or ``None`` if there is none.

    The latest answer, or the one at ``message_index``. ``at_or_before`` (an ISO time) takes the
    latest answer written by then, so a run's result is a step's answer as it stood when the step
    first finished, not whatever the user did in that conversation afterwards (workflow-runs §8).
    A message whose time can't be read is not ruled out by it.
    """
    from apis.shared.sessions.messages import get_messages
    from apis.shared.sessions.rewind import parse_message_index

    cutoff = _parse_time(at_or_before) if at_or_before else None
    try:
        listing = await get_messages(session_id=session_id, user_id=user_id)
    except FileNotFoundError:
        listing = None
    for message in reversed(listing.messages if listing else []):
        if message.role != "assistant":
            continue
        index = parse_message_index(session_id, message.id)
        if index is None or (message_index is not None and index != message_index):
            continue
        written = _parse_time(message.created_at) if cutoff is not None and message.created_at else None
        if cutoff is not None and written is not None and written > cutoff:
            continue
        return message.model_dump(by_alias=True, exclude_none=True), index
    return None


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def offered_files(user_id: str, session_id: str, *, created_before: Optional[str] = None) -> List[DeliveryFile]:
    """The files a send-back from ``session_id`` offers: its own ``READY`` files, newest first.

    A handoff's target is a brand-new conversation, so every file of its own was made after the
    handoff was accepted, which is the boundary §4.9 draws. Reference rows are left out: they came
    from elsewhere, and a file handed back is offered where it was made. ``created_before`` (an ISO
    time) leaves out files made after it, as ``answer_message``'s ``at_or_before`` does.
    """
    from apis.shared.files.models import FileStatus, is_linked_reference
    from apis.shared.files.repository import get_file_upload_repository

    try:
        rows = get_file_upload_repository().list_session_files_sync(session_id, FileStatus.READY)
    except Exception:  # noqa: BLE001 - an offer, never a failed send-back
        logger.warning("send-back: listing the conversation's files failed; none offered", exc_info=True)
        return []
    cutoff = _parse_time(created_before) if created_before else None
    own = sorted(
        (
            m
            for m in rows
            if m.user_id == user_id
            and not is_linked_reference(m)
            and (cutoff is None or (_parse_time(m.created_at) or cutoff) <= cutoff)
        ),
        key=lambda m: m.created_at,
        reverse=True,
    )
    return [_delivery_file(m, holder_title="") for m in own[:DELIVERY_MAX_FILES]]


def offered_artifacts(user_id: str, session_id: str, message_index: int) -> List[DeliveryArtifact]:
    """The artifacts a message produced, at HEAD, newest first: what a send-back of it offers."""
    heads = _artifact_heads(user_id, session_id)
    return [_artifact(h) for h in heads if h.get("produced_by_message_index") == message_index][
        :DELIVERY_MAX_ARTIFACTS
    ]


async def create_delivery(
    *,
    user_id: str,
    to_session_id: str,
    from_session_id: str = "",
    text: str = "",
    origin: DeliveryOrigin = "user",
    handoff_id: Optional[str] = None,
    artifact_ids: Sequence[str] = (),
    file_ids: Sequence[str] = (),
    parts: Sequence[DeliveryPartRequest] = (),
    run_id: Optional[str] = None,
    delivery_id: Optional[str] = None,
    pair: Optional[LinkedPair] = None,
    store: Optional[DeliveryStore] = None,
) -> Delivery:
    """Write a ``pending`` delivery into ``to_session_id``, or raise ``DeliveryRefused``.

    ``pair`` skips the session reads when the caller already made them with ``load_linked_pair``.
    ``parts`` makes a run result instead (see ``prepare_delivery``).
    """
    row = await prepare_delivery(
        user_id=user_id,
        from_session_id=from_session_id,
        to_session_id=to_session_id,
        text=text,
        origin=origin,
        handoff_id=handoff_id,
        artifact_ids=artifact_ids,
        file_ids=file_ids,
        parts=parts,
        run_id=run_id,
        delivery_id=delivery_id,
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
    files = delivery_file_count(row)
    if files:
        emit_handoff_metric("DeliveryFiles", "send_back", session_id=row.to_session_id, count=files)
    if row.parts:
        emit_handoff_metric("DeliveryParts", "send_back", session_id=row.to_session_id, count=len(row.parts))
    return row


async def prepare_delivery(
    *,
    user_id: str,
    to_session_id: str,
    from_session_id: str = "",
    text: str = "",
    origin: DeliveryOrigin = "user",
    handoff_id: Optional[str] = None,
    artifact_ids: Sequence[str] = (),
    file_ids: Sequence[str] = (),
    parts: Sequence[DeliveryPartRequest] = (),
    run_id: Optional[str] = None,
    delivery_id: Optional[str] = None,
    pair: Optional[LinkedPair] = None,
) -> Delivery:
    """Every check ``create_delivery`` makes, and the row it would write, without writing it.

    For a caller that has its own exactly-once step to take between the checks and the write (a
    send-back closes its ``HANDOFF#`` row there; a run's collect claims its result slot), so a
    refusal never leaves that step taken.

    With ``parts`` the row is a run result: each part is checked against its own sending
    conversation by the same rules as a single card, and the row-level text, artifacts and files
    stay empty. ``delivery_id`` names the row when the caller had to know it first.
    """
    if parts:
        if from_session_id or text or artifact_ids or file_ids or pair is not None:
            raise ValueError("a run result carries everything in its parts")
        return await _prepare_run_result(
            user_id=user_id,
            to_session_id=to_session_id,
            parts=parts,
            origin=origin,
            run_id=run_id,
            delivery_id=delivery_id,
        )
    if not from_session_id:
        raise ValueError("a delivery names the conversation it is from")
    pair = pair or await load_linked_pair(user_id, from_session_id, to_session_id)
    sender_title = str(pair.sender.get("title") or "")
    body, artifacts, files = await _checked_content(
        user_id, from_session_id, sender_title, text=text, artifact_ids=artifact_ids, file_ids=file_ids
    )
    if not body and not artifacts and not files:
        raise DeliveryRefused("empty")

    now = utc_now_iso()
    return Delivery(
        delivery_id=delivery_id or str(uuid.uuid4()),
        user_id=user_id,
        to_session_id=to_session_id,
        from_session_id=from_session_id,
        from_title=sender_title,
        handoff_id=handoff_id,
        origin=origin,
        text=body,
        text_hash=context_hash(body) if body else "",
        artifacts=artifacts,
        files=files,
        run_id=run_id,
        status="pending",
        created_at=now,
        updated_at=now,
    )


async def _prepare_run_result(
    *,
    user_id: str,
    to_session_id: str,
    parts: Sequence[DeliveryPartRequest],
    origin: DeliveryOrigin,
    run_id: Optional[str],
    delivery_id: Optional[str],
) -> Delivery:
    """A run result's row: ``_checked_content`` per part, the receiver read once."""
    if len(parts) > DELIVERY_MAX_PARTS:
        raise DeliveryRefused("too_many_parts")
    if len({p.step_id for p in parts}) != len(parts):
        raise ValueError("each step is one part")
    for part in parts:
        if part.status != "done" and (part.text or part.artifact_ids or part.file_ids):
            raise ValueError("only a finished step carries a result")
        if part.prompt and (part.from_session_id or part.artifact_ids or part.file_ids):
            raise ValueError("a prompt step's part is text alone, from no conversation")
    if sum(len(set(p.file_ids)) for p in parts) > RUN_RESULT_MAX_FILES:
        raise DeliveryRefused("too_many_files")

    # Only a finished part carries anything across, so only its conversation is a sender, held to
    # every rule. A part with no result names its step and Agent and reads nothing. A prompt step's
    # part has no conversation (workflow-designer §21.3): its text is the step's own answer, made
    # inside this run, so it has no sender to check, and only the receiver is held to the rules.
    senders = [p.from_session_id for p in parts if p.status == "done" and not p.prompt and p.from_session_id]
    if any(p.status == "done" and not p.prompt and not p.from_session_id for p in parts):
        raise ValueError("a finished step names its conversation")
    text_only = any(p.status == "done" and p.prompt for p in parts)
    if not senders and not text_only:
        raise DeliveryRefused("empty")
    if senders:
        pairs = await load_linked_senders(user_id, senders, to_session_id)
    else:
        await load_receiver(user_id, to_session_id)
        pairs = {}

    def title_of(part: DeliveryPartRequest) -> str:
        if part.prompt:
            return part.from_title
        pair = pairs.get(part.from_session_id) if part.status == "done" else None
        return str(pair.sender.get("title") or "") if pair is not None else ""

    async def content_of(part: DeliveryPartRequest) -> Tuple[str, List[DeliveryArtifact], List[DeliveryFile]]:
        if part.prompt:
            return cap_delivery_text(part.text), [], []
        return await _checked_content(
            user_id,
            part.from_session_id,
            title_of(part),
            text=part.text,
            artifact_ids=part.artifact_ids,
            file_ids=part.file_ids,
        )

    contents = await asyncio.gather(*(content_of(part) for part in parts))
    built = [
        DeliveryPart(
            step_id=part.step_id,
            agent_name=part.agent_name,
            from_session_id=part.from_session_id,
            from_title=title_of(part),
            status=part.status,
            text=body,
            text_hash=context_hash(body) if body else "",
            artifacts=artifacts,
            files=files,
            kind="prompt" if part.prompt else None,
        )
        for part, (body, artifacts, files) in zip(parts, contents)
    ]
    if not any(p.text or p.artifacts or p.files for p in built):
        raise DeliveryRefused("empty")

    now = utc_now_iso()
    done = [p for p in built if p.status == "done"]
    # A conversation to name when there is one: a prompt step's part has none.
    first = next((p for p in done if p.from_session_id), done[0])
    return Delivery(
        delivery_id=delivery_id or str(uuid.uuid4()),
        user_id=user_id,
        to_session_id=to_session_id,
        # A reader that doesn't know parts sees the first step; the model only ever reads the parts.
        from_session_id=first.from_session_id,
        from_title=first.from_title or first.agent_name,
        origin=origin,
        run_id=run_id,
        parts=built,
        status="pending",
        created_at=now,
        updated_at=now,
    )


async def _checked_content(
    user_id: str,
    sender_session_id: str,
    sender_title: str,
    *,
    text: str,
    artifact_ids: Sequence[str],
    file_ids: Sequence[str],
) -> Tuple[str, List[DeliveryArtifact], List[DeliveryFile]]:
    """One sender's text, artifacts and files, each held to §4.8's and §4.9's rules.

    The text capped; every artifact made by the caller in ``sender_session_id``; every file the
    caller's, ``READY`` and a row of that conversation, carried as its original. A single card and
    every part of a run result go through here, so the rules exist once.
    """
    wanted = list(dict.fromkeys(artifact_ids))
    if len(wanted) > DELIVERY_MAX_ARTIFACTS:
        raise DeliveryRefused("too_many_artifacts")
    artifacts: List[DeliveryArtifact] = []
    if wanted:
        heads = {h["artifact_id"]: h for h in await asyncio.to_thread(_artifact_heads, user_id, sender_session_id)}
        missing = [a for a in wanted if a not in heads]
        if missing:
            # Another user's, from another conversation, or gone: one answer for all three.
            raise DeliveryRefused("artifact_not_in_sender")
        artifacts = [_artifact(heads[a]) for a in wanted]
    files = await _sender_files(user_id, sender_session_id, file_ids, sender_title=sender_title)
    return cap_delivery_text(text), artifacts, files


async def _sender_files(
    user_id: str, sender_session_id: str, file_ids: Sequence[str], *, sender_title: str
) -> List[DeliveryFile]:
    """The files a delivery carries, each checked against the sender and resolved to its original.

    One ``BatchGetItem``. Any file that fails §4.8's sender rule refuses the whole delivery, as an
    artifact does: the user picked it, so dropping it silently would send less than they chose.
    """
    wanted = list(dict.fromkeys(f for f in file_ids if f))
    if not wanted:
        return []
    if len(wanted) > DELIVERY_MAX_FILES:
        raise DeliveryRefused("too_many_files")
    from apis.shared.files.repository import get_file_upload_repository

    rows = await asyncio.to_thread(get_file_upload_repository().batch_get_files_sync, user_id, wanted)
    files: List[DeliveryFile] = []
    for upload_id in wanted:
        meta = rows.get(upload_id)
        reason = linkable_refusal(meta, user_id=user_id, sender_session_id=sender_session_id)
        if reason is not None:
            raise DeliveryRefused("file_not_in_sender")
        assert meta is not None
        files.append(_delivery_file(meta, holder_title=sender_title))
    return files


def _delivery_file(meta: Any, *, holder_title: str) -> DeliveryFile:
    original = original_of(meta, holder_title=holder_title)
    return DeliveryFile(
        upload_id=original.upload_id,
        session_id=original.session_id,
        from_title=original.from_title,
        filename=original.filename,
        mime_type=original.mime_type,
        size_bytes=original.size_bytes,
        s3_key=original.s3_key,
        s3_bucket=original.s3_bucket,
        ttl=original.ttl,
    )


def project_of(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The Shared Project a ``META`` row's conversation is a task of, or ``None``.

    The session row keeps it under ``preferences.projectId``; a top-level ``projectId`` is read
    too, so a row written either way is refused.
    """
    if not row:
        return None
    top = row.get("projectId")
    if isinstance(top, str) and top:
        return top
    prefs = row.get("preferences")
    nested = prefs.get("projectId") if isinstance(prefs, Mapping) else None
    return nested if isinstance(nested, str) and nested else None


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
    "DeliveryPartRequest",
    "DeliveryRefused",
    "LinkedPair",
    "answer_message",
    "create_delivery",
    "load_linked_pair",
    "load_linked_senders",
    "load_receiver",
    "offered_artifacts",
    "offered_files",
    "prepare_delivery",
    "project_of",
    "render_return_excerpt",
    "write_delivery",
]
