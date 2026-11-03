"""Fork a conversation from one of the user's earlier messages.

``docs/specs/conversation-rewind-and-fork.md`` §7. A fork is a new session
holding a copy of everything the parent shows before the chosen user message;
the parent is untouched. The message's text comes back for the new session's
composer, so a fork is a rewind into a new session.

**The copy is verbatim and never re-extracted.** Each stored message is written
into the child's AgentCore Memory session exactly as the parent stored it, with
``extractionMode="SKIP"``: the user already owns these memories once, and
re-extracting them would duplicate long-term records and pay for it (§7.2).
The child's physical indices are contiguous from 0, so logical equals physical
and the parent's compaction state carries over after the §5 rules for a cut.

**The prompt cache is the Runtime's half.** A restore from these stored copies
is not byte-identical to what the parent sent: long-term-memory context is
spliced into each user message when it is sent and never stored (§2.3). So the
child records where it came from (``forkedFrom``) and which conversation its
family started from (``rootSessionId``). The SPA pins the child's turns to the
root's container, and the Runtime starts the child's first turn from an exact
copy of the parent's live history there, which is the prefix Bedrock cached.

Off the turn path, in app-api. The parent is only read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.aws_clients import get_dynamodb_table
from apis.shared.sessions.fork import FORKED_FROM_ATTRIBUTE, ROOT_SESSION_ATTRIBUTE, ForkOrigin, root_session_of
from apis.shared.sessions.messages import list_stored_messages
from apis.shared.sessions.metadata import (
    _get_session_by_gsi,
    _static_session_sk,
    get_all_message_metadata,
    get_session_metadata,
    store_session_metadata,
    store_user_display_text,
)
from apis.shared.sessions.models import SessionMetadata
from apis.shared.sessions.rewind import RewindState, is_rewind_target, parse_message_index

from .rewind_service import RewindRefused, _compaction_after_cut, _prefill_text, _to_dynamo

logger = logging.getLogger(__name__)

# The agent id Strands restores under. The runtime never passes one.
_FORK_AGENT_ID = "default"


@dataclass(frozen=True)
class ForkResult:
    session_id: str
    title: str
    prefill_text: str
    root_session_id: str
    copied_messages: int


def _copy_messages(session_id: str, user_id: str, messages: List[Dict[str, Any]]) -> None:
    """Write ``messages`` into a new Memory session, in order, skipping extraction. Blocking.

    The AGENT record goes first: Strands restores a session's messages only
    when it finds one, and without it the fork's first cold turn would take the
    new-agent branch and run on an empty history (the share fork learned this,
    ``ShareService._copy_messages_to_memory``). Raises on any failed write, so a
    partial copy never gets a session row pointing at it.
    """
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    from strands.agent.conversation_manager import SlidingWindowConversationManager
    from strands.types.session import SessionAgent, SessionMessage

    from apis.app_api.shares.service import _skip_long_term_extraction

    memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
    if not memory_id:
        raise RewindRefused(503, "Conversation storage is not configured")
    mgr = AgentCoreMemorySessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id=memory_id, session_id=session_id, actor_id=user_id, enable_prompt_caching=False
        ),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )
    _skip_long_term_extraction(mgr)
    # Must name the class the runtime builds, or restore raises on the mismatch
    # (test_fork_agent_record_restores_into_the_runtime_manager pins it).
    mgr.create_agent(
        session_id,
        SessionAgent(
            agent_id=_FORK_AGENT_ID,
            state={},
            conversation_manager_state=SlidingWindowConversationManager().get_state(),
        ),
    )
    for index, message in enumerate(messages):
        mgr.create_message(session_id, _FORK_AGENT_ID, SessionMessage.from_message(message, index=index))


def _plan(
    session_id: str, user_id: str, physical_cut: int, row: Dict[str, Any]
) -> Tuple[List[Tuple[int, Dict[str, Any]]], Dict[str, Any], RewindState, Optional[Dict[str, Any]]]:
    """What to copy: the visible messages before the cut, the target, and the child's compaction. Blocking."""
    stored = list_stored_messages(session_id, user_id)
    if not 0 <= physical_cut < len(stored):
        raise RewindRefused(404, "Message not found")
    state = RewindState.from_row(row)
    if state.is_hidden(physical_cut):
        raise RewindRefused(409, "That message has been rewound away")
    target = stored[physical_cut]
    if not is_rewind_target(target):
        raise RewindRefused(400, "Only your own messages can be forked from")
    kept = [(i, m) for i, m in state.visible(stored) if i < physical_cut]
    compaction = _compaction_after_cut(row.get("compaction"), len(kept))
    if compaction is not None:
        # A fresh stamp: the child's first restore must not read the parent's
        # turn gap as its own and slide the truncation anchor, which would
        # truncate tool output the parent sent whole.
        compaction["updatedAt"] = datetime.now(timezone.utc).isoformat()
    return kept, target, state, compaction


async def fork_session(session_id: str, user_id: str, message_id: str) -> ForkResult:
    """Fork ``session_id`` into a new session holding everything before ``message_id``.

    Raises ``RewindRefused`` with the status to answer.
    """
    physical_cut = parse_message_index(session_id, message_id)
    if physical_cut is None:
        raise RewindRefused(400, "Not a message in this conversation")
    table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not table_name:
        raise RewindRefused(503, "Session storage is not configured")
    table = get_dynamodb_table(table_name)

    row = await _get_session_by_gsi(session_id, user_id, table)
    parent = await get_session_metadata(session_id, user_id)
    if not row or parent is None:
        raise RewindRefused(404, "Session not found")

    kept, target, state, compaction = await asyncio.to_thread(_plan, session_id, user_id, physical_cut, row)
    child_id = str(uuid.uuid4())
    copied = [message for _, message in kept]
    if copied:
        try:
            await asyncio.to_thread(_copy_messages, child_id, user_id, copied)
        except RewindRefused:
            raise
        except Exception as e:
            logger.error("Fork copy failed after creating its Memory session", exc_info=True)
            raise RewindRefused(500, "Couldn't copy the conversation") from e

    now = datetime.now(timezone.utc).isoformat()
    title = f"{parent.title} (fork)"
    root = root_session_of(session_id, row)
    await store_session_metadata(
        session_id=child_id,
        user_id=user_id,
        session_metadata=SessionMetadata(
            session_id=child_id,
            user_id=user_id,
            title=title,
            status="active",
            created_at=now,
            last_message_at=now,
            message_count=len(copied),
            preferences=parent.preferences,
        ),
    )

    origin = ForkOrigin(
        session_id=session_id,
        message_index=physical_cut,
        cut=len(copied),
        parent_rewind_epoch=state.epoch,
    )
    names = {"#ff": FORKED_FROM_ATTRIBUTE, "#root": ROOT_SESSION_ATTRIBUTE}
    values: Dict[str, Any] = {":ff": origin.to_dict(), ":root": root}
    expression = "SET #ff = :ff, #root = :root"
    if compaction is not None:
        names["#cmp"] = "compaction"
        values[":cmp"] = _to_dynamo(compaction)
        expression += ", #cmp = :cmp"
    await asyncio.to_thread(
        table.update_item,
        Key={"PK": f"USER#{user_id}", "SK": _static_session_sk(child_id)},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )

    await _copy_display_texts(session_id, child_id, user_id, kept)
    _archive(child_id, user_id, copied, parent.preferences)

    logger.info(
        "Forked session at physical %d: %d message(s) copied into a new session",
        physical_cut, len(copied),
    )
    prefill = await asyncio.to_thread(_prefill_text, session_id, user_id, physical_cut, target)
    return ForkResult(
        session_id=child_id,
        title=title,
        prefill_text=prefill,
        root_session_id=root,
        copied_messages=len(copied),
    )


async def _copy_display_texts(
    parent_id: str, child_id: str, user_id: str, kept: List[Tuple[int, Dict[str, Any]]]
) -> None:
    """Carry each copied user message's ``displayText`` to its new index. Best-effort.

    Without it a reloaded fork shows the augmented prompt the model was sent
    (attachment markers, folder manifests) where the parent shows what the
    user typed.
    """
    try:
        index = await get_all_message_metadata(parent_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("Fork: could not read the parent's display texts", exc_info=True)
        return
    for new_index, (old_index, message) in enumerate(kept):
        if message.get("role") != "user":
            continue
        text = (index.get(str(old_index)) or {}).get("displayText")
        if isinstance(text, str) and text.strip():
            await store_user_display_text(child_id, user_id, new_index, text)


def _archive(child_id: str, user_id: str, copied: List[Dict[str, Any]], preferences: Any) -> None:
    """Index the fork's copied turns for conversation search, as the share fork does."""
    from apis.app_api.shares.service import ShareService

    ShareService._archive_forked_turns(child_id, user_id, copied, preferences)
