"""Share service layer

Business logic for creating, retrieving, updating, and revoking
conversation share snapshots.  Supports multiple shares per session.
"""

import json
import logging
import os
import re
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from apis.shared.audit import TARGET_PROJECT, AuditAction, AuditService, get_audit_service
from apis.shared.auth.models import User
from apis.shared.feature_flags import projects_enabled
from apis.shared.notifications.service import NotificationService
from apis.shared.projects.access import resolve_project_role
from apis.shared.projects.models import Project, SharedTask, normalize_email
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import max_members
from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.metadata import get_session_metadata, store_session_metadata

from .models import (
    CreateShareRequest,
    ShareListResponse,
    ShareResponse,
    SharedConversationArtifact,
    SharedConversationResponse,
    UpdateShareRequest,
)
from apis.shared.shares.snapshots import SnapshotUnreadableError, convert_decimals_to_float, load_snapshot_raw
from apis.shared.shares.snapshot_store import (
    ShareSnapshotStore,
    ShareSnapshotStoreError,
    get_share_snapshot_store,
)

# Snapshot schema version stamped on the S3 body pointer, for forward
# migration if the body shape ever changes.
_SNAPSHOT_SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)

# The ``[Attached files: …]`` marker ``PromptBuilder.build_prompt`` appends to
# a user message that carried attachments. Same shape the SPA's
# ``ATTACHED_FILES_PATTERN`` matches (end-anchored).
_ATTACHED_FILES_MARKER = re.compile(r"\n*\[Attached files: ([^\]]+)\]\s*$")

# Strands' default agent id — the runtime never passes one, so every session's
# messages and its AGENT record live under it.
_FORK_AGENT_ID = "default"


def _attachments_not_copied_note(names: List[str]) -> str:
    """Text that stands in for a fork's attachments.

    A fork carries no session files. They are the owner's uploads, and copying
    them would put another person's files in the forker's storage and quota —
    more than sharing a conversation should do. Both the forker and the model
    read this line, so neither expects to re-open them.
    """
    return f"[Attachments from the original conversation were not copied: {', '.join(names)}]"


def _media_not_copied_text(key: str, media: Any) -> str:
    """Placeholder for an image/document nested in a copied tool result."""
    name = media.get("name") if isinstance(media, dict) else None
    label = f'{key} "{name}"' if name else key
    return f"[{label.capitalize()} from the original conversation was not copied]"


def _strip_media_from_tool_result(tool_result: Any) -> Any:
    """Replace images/documents inside a tool result with text placeholders.

    A snapshot carries them in the display shape (``format``/``data``, no
    ``source``), which Bedrock rejects as a Converse block.
    """
    if not isinstance(tool_result, dict) or not isinstance(tool_result.get("content"), list):
        return tool_result
    content = []
    for item in tool_result["content"]:
        if isinstance(item, dict) and ("image" in item or "document" in item):
            key = "image" if "image" in item else "document"
            content.append({"text": _media_not_copied_text(key, item[key])})
        else:
            content.append(item)
    return {**tool_result, "content": content}


def _skip_long_term_extraction(mgr: Any) -> None:
    """Make every event ``mgr`` writes skip long-term memory extraction.

    ``AgentCoreMemorySessionManager.create_message`` has no extraction-mode
    argument, but all of its writes (conversational events via
    ``MemoryClient.create_event`` and oversized blob events) end in the data
    plane client's ``create_event``. Wrapping that one call on this manager's
    own client sets ``extractionMode="SKIP"`` without copying the SDK's
    message conversion. The manager is built per fork, so nothing else shares
    the wrapped client.
    """
    gmdp = mgr.memory_client.gmdp_client
    original = gmdp.create_event

    def create_event(**kwargs: Any) -> Any:
        kwargs.setdefault("extractionMode", "SKIP")
        return original(**kwargs)

    gmdp.create_event = create_event


def _run_now(fn: Callable[..., Any], *args: Any) -> None:
    fn(*args)


class ShareService:
    """Handles share CRUD operations against the shared-conversations DynamoDB table."""

    def __init__(
        self,
        snapshot_store: Optional[ShareSnapshotStore] = None,
        project_repository: Optional[ProjectRepository] = None,
        audit: Optional[AuditService] = None,
        notifications: Optional[NotificationService] = None,
    ) -> None:
        table_name = os.environ.get("SHARED_CONVERSATIONS_TABLE_NAME", "")
        # Built on first use: most shares never touch a project.
        self._project_repository = project_repository
        self._audit = audit
        self._notifications = notifications
        self._table_name = table_name
        self._enabled = bool(table_name)
        # S3-backed snapshot body store. Injectable for tests; otherwise the
        # process-global store (bucket from SHARED_CONVERSATIONS_BUCKET_NAME).
        self._snapshot_store = snapshot_store or get_share_snapshot_store()

        if self._enabled:
            self._dynamodb = boto3.resource("dynamodb")
            self._table = self._dynamodb.Table(table_name)
            logger.info(f"ShareService initialized with table: {table_name}")
        else:
            self._dynamodb = None
            self._table = None
            logger.warning("ShareService disabled - SHARED_CONVERSATIONS_TABLE_NAME not set")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_share(
        self,
        session_id: str,
        user: User,
        request: CreateShareRequest,
        schedule: Optional[Callable[..., Any]] = None,
    ) -> ShareResponse:
        """Create a new share snapshot for a session.

        Multiple shares can exist per session (e.g. after continuing a conversation).

        A project share's ``notify`` fan-out is handed to ``schedule`` (the route's
        ``BackgroundTasks.add_task``) so the inbox writes never delay the response;
        without one it runs inline.
        """
        self._ensure_enabled()

        # Verify session ownership
        metadata = await get_session_metadata(session_id=session_id, user_id=user.user_id)
        if not metadata:
            raise SessionNotFoundError(session_id)

        project: Optional[Project] = None
        project_id = None
        recipients: List[str] = []
        if request.access_level == "project":
            project = self._require_shareable_project(metadata, user)
            project_id = project.project_id
            # Before anything is written: a bad recipient is a 400 with no share made.
            recipients = self._notify_recipients(project, request, user)

        # Snapshot messages
        messages_response = await get_messages(session_id=session_id, user_id=user.user_id)
        messages_snapshot = [
            msg.model_dump(by_alias=True, exclude_none=True)
            for msg in messages_response.messages
        ]

        metadata_snapshot = metadata.model_dump(by_alias=True, exclude_none=True)

        share_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        allowed_emails = self._resolve_allowed_emails(
            request.access_level, request.allowed_emails, user.email
        )

        # Offload the snapshot BODY (messages + metadata) to S3 — a long
        # conversation exceeds DynamoDB's 400 KB item limit if inlined. The
        # DynamoDB row keeps only the small control fields plus a pointer.
        # The body is opaque JSON bytes in S3, so we serialize the raw
        # model_dump directly and skip the float→Decimal dance (that only
        # exists to satisfy DynamoDB's boto3 resource, which rejects floats).
        if not self._snapshot_store.enabled:
            raise ShareStorageUnavailableError()

        # Artifacts the conversation produced, pinned at the version each
        # stood at right now.
        #
        # They belong in the snapshot for the same reason the messages
        # do: this share is a point-in-time copy, and an artifact
        # resolved live would drift under a recipient who is reading a
        # frozen conversation — they would see a chart the transcript
        # around it never describes. Pinning also makes the snapshot the
        # ALLOWLIST: the mint route will serve an (artifact, version)
        # pair only if it appears here, so a recipient cannot name an
        # arbitrary artifact of the owner's.
        artifacts_snapshot = self._snapshot_artifacts(session_id, user)

        body_bytes = json.dumps(
            {
                "metadata": metadata_snapshot,
                "messages": messages_snapshot,
                "artifacts": artifacts_snapshot,
                # The model's one-line tool-batch summaries, frozen with the
                # messages they describe. They live in TSUM# rows, not on the
                # messages, so without this a snapshot falls back to the
                # client-side wording ("Ran memory read").
                "toolSummaries": messages_response.tool_summaries,
            }
        ).encode("utf-8")

        try:
            bucket_key = self._snapshot_store.put(share_id=share_id, body=body_bytes)
        except ShareSnapshotStoreError as e:
            logger.error(
                f"Failed to store snapshot body for share {self._sanitize_id(share_id)}: {e}"
            )
            raise ShareStorageUnavailableError() from e

        item = {
            "share_id": share_id,
            "session_id": session_id,
            "owner_id": user.user_id,
            "owner_email": user.email,
            "access_level": request.access_level,
            "created_at": now,
            # Denormalized from the snapshot so a project pointer can be rebuilt
            # from the row alone.
            "title": str(metadata_snapshot.get("title") or ""),
            "body_ref": {
                "bucket_key": bucket_key,
                "format": "json",
                "schema_version": _SNAPSHOT_SCHEMA_VERSION,
                "byte_size": len(body_bytes),
            },
        }
        if allowed_emails is not None:
            item["allowed_emails"] = allowed_emails
        if project_id:
            item["project_id"] = project_id
        if request.note:
            item["note"] = request.note

        self._table.put_item(Item=item)
        if project_id:
            # Without its pointer a project share is unlisted, so the two land
            # together or not at all.
            try:
                self._put_project_pointer(item)
            except Exception:
                logger.error(
                    f"Project pointer write failed for share {self._sanitize_id(share_id)}; rolling back",
                    exc_info=True,
                )
                self._table.delete_item(Key={"share_id": share_id})
                self._delete_snapshot_body(item)
                raise
            self._record_task_share(
                AuditAction.PROJECT_TASK_SHARED, user, item, notified=len(recipients), hasNote=bool(request.note)
            )
            if recipients:
                (schedule or _run_now)(self._notify_task_shared, recipients, user, project, item)
        logger.info(f"Created share {self._sanitize_id(share_id)} for session {self._sanitize_id(session_id)}")

        return self._build_share_response(item)

    async def get_shared_conversation(
        self,
        share_id: str,
        requester: User,
    ) -> SharedConversationResponse:
        """Retrieve a shared conversation snapshot, enforcing access control."""
        self._ensure_enabled()

        item = self._get_share_item(share_id)
        if not item:
            raise ShareNotFoundError()

        self._check_access(item, requester)

        return self._build_shared_conversation_response(item)

    async def update_share(
        self,
        share_id: str,
        user: User,
        request: UpdateShareRequest,
    ) -> ShareResponse:
        """Update access level / allowed emails on an existing share."""
        self._ensure_enabled()

        item = self._get_share_item(share_id)
        if not item:
            raise ShareNotFoundError()

        if item["owner_id"] != user.user_id:
            raise NotOwnerError()

        update_expr_parts: list[str] = []
        attr_values: dict = {}
        remove_parts: list[str] = []

        old_access = item.get("access_level")
        old_project_id = item.get("project_id")
        new_access = request.access_level or old_access

        if request.access_level is not None:
            update_expr_parts.append("access_level = :al")
            attr_values[":al"] = request.access_level

        if new_access == "project" and old_access != "project":
            metadata = await get_session_metadata(session_id=item["session_id"], user_id=user.user_id)
            if not metadata:
                raise SessionNotFoundError(item["session_id"])
            update_expr_parts.append("project_id = :pid")
            attr_values[":pid"] = self._require_shareable_project(metadata, user).project_id
        elif new_access != "project" and old_project_id:
            remove_parts.append("project_id")
            if "note" in item:
                remove_parts.append("note")

        # Resolve allowed_emails
        if new_access == "specific":
            emails = request.allowed_emails or item.get("allowed_emails", [])
            resolved = self._resolve_allowed_emails(new_access, emails, user.email)
            update_expr_parts.append("allowed_emails = :ae")
            attr_values[":ae"] = resolved
        elif request.access_level is not None:
            # Switching to public → clear allowed_emails
            remove_parts.append("allowed_emails")

        if not update_expr_parts and not remove_parts:
            return self._build_share_response(item)

        update_expr = ""
        if update_expr_parts:
            update_expr += "SET " + ", ".join(update_expr_parts)
        if remove_parts:
            update_expr += " REMOVE " + ", ".join(remove_parts)

        kwargs = {
            "Key": {"share_id": item["share_id"]},
            "UpdateExpression": update_expr,
            "ReturnValues": "ALL_NEW",
        }
        if attr_values:
            kwargs["ExpressionAttributeValues"] = attr_values

        result = self._table.update_item(**kwargs)
        updated = result.get("Attributes", item)
        logger.info(f"Updated share {item['share_id']}")

        for project_id in {old_project_id, updated.get("project_id")} - {None}:
            self._sync_project_pointer(item["session_id"], project_id)
        new_project_id = updated.get("project_id")
        if old_project_id != new_project_id:
            if old_project_id:
                self._record_task_share(AuditAction.PROJECT_TASK_UNSHARED, user, {**item, "project_id": old_project_id})
            if new_project_id:
                self._record_task_share(AuditAction.PROJECT_TASK_SHARED, user, updated)

        return self._build_share_response(updated)

    async def revoke_share(self, share_id: str, user: User) -> None:
        """Delete a specific share by share_id."""
        self._ensure_enabled()

        item = self._get_share_item(share_id)
        if not item:
            raise ShareNotFoundError()

        if item["owner_id"] != user.user_id:
            raise NotOwnerError()

        self._table.delete_item(Key={"share_id": item["share_id"]})
        self._delete_snapshot_body(item)
        if item.get("project_id"):
            self._sync_project_pointer(item["session_id"], item["project_id"])
            self._record_task_share(AuditAction.PROJECT_TASK_UNSHARED, user, item)
        logger.info(f"Revoked share {item['share_id']}")

    async def delete_shares_for_session(self, session_id: str) -> int:
        """Delete all share snapshots for a session.

        Called as a background task when the session owner deletes a conversation.
        Removes share records so that existing share links stop working.
        Exported conversations (copied into recipients' own sessions) are unaffected.

        Returns:
            Number of shares deleted.
        """
        if not self._enabled:
            logger.debug("ShareService disabled - skipping share cleanup")
            return 0

        try:
            items = self._find_shares_by_session(session_id)
            if not items:
                return 0

            # Batch delete all shares for this session
            with self._table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"share_id": item["share_id"]})

            # Best-effort cleanup of each share's S3 snapshot body. The
            # DynamoDB delete above is what makes the share link stop working;
            # an S3 miss here only leaves an orphan object, never a live share.
            for item in items:
                self._delete_snapshot_body(item)

            for project_id in {i.get("project_id") for i in items} - {None}:
                self._sync_project_pointer(session_id, project_id)

            logger.info(
                f"Deleted {len(items)} share(s) for session "
                f"{self._sanitize_id(session_id)}"
            )
            return len(items)

        except Exception:
            logger.error(
                "Failed to delete shares for session "
                f"{self._sanitize_id(session_id)}",
                exc_info=True,
            )
            return 0

    async def get_shares_for_session(self, session_id: str, user_id: str) -> ShareListResponse:
        """Return all shares for a session owned by the user."""
        self._ensure_enabled()

        items = self._find_shares_by_session(session_id)
        shares = [
            self._build_share_response(item)
            for item in items
            if item["owner_id"] == user_id
        ]
        return ShareListResponse(shares=shares)

    async def export_shared_conversation(
        self,
        share_id: str,
        requester: User,
    ) -> dict:
        """Export a shared conversation as a new session for the requester.

        Creates a new session with the snapshot messages copied into AgentCore
        Memory, producing a full fork of the shared conversation.
        """
        self._ensure_enabled()

        item = self._get_share_item(share_id)
        if not item:
            raise ShareNotFoundError()

        self._check_access(item, requester)

        metadata, snapshot_messages = self._load_snapshot_body(item)
        original_title = metadata.get("title", "Untitled Conversation")
        new_title = f"{original_title} (shared)"

        new_session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Copy snapshot messages into AgentCore Memory for the new session
        message_count = await self._copy_messages_to_memory(
            new_session_id, requester.user_id, snapshot_messages
        )

        from apis.shared.sessions.models import SessionMetadata

        session_meta = SessionMetadata(
            session_id=new_session_id,
            user_id=requester.user_id,
            title=new_title,
            status="active",
            created_at=now,
            last_message_at=now,
            message_count=message_count,
            preferences=self._forked_project_preferences(metadata, requester),
        )

        await store_session_metadata(
            session_id=new_session_id,
            user_id=requester.user_id,
            session_metadata=session_meta,
        )

        logger.info(
            f"Exported share {self._sanitize_id(share_id)} to new session {self._sanitize_id(new_session_id)} "
            f"for user {self._sanitize_id(requester.user_id)} ({message_count} messages copied)"
        )

        return {"sessionId": new_session_id, "title": new_title}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Message copying helpers

    async def _copy_messages_to_memory(
        self,
        session_id: str,
        user_id: str,
        snapshot_messages: list,
    ) -> int:
        """Write snapshot messages into AgentCore Memory for a new session.

        Converts each MessageResponse dict to SessionMessage format and
        persists each via create_message under the default agent id.

        The messages were written by someone else (the share's owner), but
        they land under the forking user's actor. Every event is therefore
        written with ``extractionMode="SKIP"``: it stays in short-term memory,
        so the fork's history loads, but it never feeds long-term extraction,
        so another person's content does not become the forker's "memories".

        The session's AGENT record is written first. Strands'
        ``RepositorySessionManager.initialize`` loads a session's messages only
        when it finds that record; without it the fork's first turn took the
        new-agent branch and ran on an empty history ("Restore @init: 0
        messages") even though the copied conversation was on screen.

        Returns:
            Number of messages successfully written.
        """
        if not snapshot_messages:
            return 0

        import asyncio

        try:
            from bedrock_agentcore.memory.integrations.strands.config import (
                AgentCoreMemoryConfig,
            )
            from bedrock_agentcore.memory.integrations.strands.session_manager import (
                AgentCoreMemorySessionManager,
            )
            from strands.agent.conversation_manager import SlidingWindowConversationManager
            from strands.types.session import SessionAgent, SessionMessage
        except ImportError:
            logger.error("AgentCore Memory SDK not available — cannot copy messages")
            return 0

        memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
        aws_region = os.environ.get("AWS_REGION", "us-west-2")
        if not memory_id:
            logger.error("AGENTCORE_MEMORY_ID not set — cannot copy messages")
            return 0

        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=user_id,
            enable_prompt_caching=False,
        )
        mgr = AgentCoreMemorySessionManager(
            agentcore_memory_config=config, region_name=aws_region
        )
        _skip_long_term_extraction(mgr)

        # What ``SessionAgent.from_agent`` records for a fresh agent. The
        # conversation-manager state must name the class the runtime builds
        # (``AgentFactory.build_conversation_manager``): restore raises on a
        # mismatch. ``test_fork_agent_record_restores_into_the_runtime_manager``
        # pins the two together.
        session_agent = SessionAgent(
            agent_id=_FORK_AGENT_ID,
            state={},
            conversation_manager_state=SlidingWindowConversationManager().get_state(),
        )
        await asyncio.to_thread(mgr.create_agent, session_id, session_agent)

        count = 0
        for idx, msg_dict in enumerate(snapshot_messages):
            converse_msg = self._snapshot_msg_to_converse(msg_dict)
            if converse_msg is None:
                continue
            try:
                # Create SessionMessage with proper index for ordering
                session_msg = SessionMessage.from_message(converse_msg, index=idx)
                await asyncio.to_thread(mgr.create_message, session_id, _FORK_AGENT_ID, session_msg)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to copy message {idx}: {e}")

        logger.info(f"Copied {count}/{len(snapshot_messages)} messages to AgentCore Memory")
        return count

    @staticmethod
    def _snapshot_msg_to_converse(msg: dict) -> Optional[dict]:
        """Convert a snapshot MessageResponse dict to Bedrock Converse format.

        Snapshot format (MessageResponse):
            {"id": "...", "role": "user", "content": [{"type": "text", "text": "hi"}, ...],
             "metadata": {"displayText": "hi"}, ...}

        Converse format (Strands/Bedrock):
            {"role": "user", "content": [{"text": "hi"}, ...]}

        The snapshot is the *display* shape of a conversation, not the model's,
        so three things are rewritten rather than copied:

        - A user message's text is its ``displayText`` when it has one — what
          the author typed, not the prompt the model saw (RAG context, notes,
          the attachments marker). The fork shows exactly what the share showed,
          and the owner's retrieved knowledge-base excerpts stay behind.
        - Attachments (document/image blocks and the ``[Attached files: …]``
          marker) become one line saying they were not copied. The snapshot's
          blocks have no ``source`` — Bedrock rejects them, which failed every
          turn of a fork once its agent was rebuilt — and the fork has no
          session files for the model to re-read.
        - Images/documents inside tool results become text placeholders, for
          the same reason.
        """
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return None

        metadata = msg.get("metadata")
        display_text = metadata.get("displayText") if isinstance(metadata, dict) else None
        use_display_text = role == "user" and isinstance(display_text, str) and bool(display_text.strip())

        converse_content: List[dict] = []
        # Index of the message's first text block: where displayText and the
        # attachments note go. Block order is otherwise kept — Claude requires a
        # user message's toolResult blocks to come before its text.
        text_at: Optional[int] = None
        marker_names: List[str] = []
        block_names: List[str] = []
        unnamed_attachments = 0

        for block in msg.get("content", []):
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text" and block.get("text"):
                text = block["text"]
                marker = _ATTACHED_FILES_MARKER.search(text)
                if marker:
                    marker_names.extend(n.strip() for n in marker.group(1).split(",") if n.strip())
                    text = text[: marker.start()]
                if use_display_text:
                    if text_at is None:
                        text_at = len(converse_content)
                        converse_content.append({"text": display_text})
                    continue
                if text.strip():
                    if text_at is None:
                        text_at = len(converse_content)
                    converse_content.append({"text": text})
            elif block_type == "toolUse" and block.get("toolUse"):
                converse_content.append({"toolUse": block["toolUse"]})
            elif block_type == "toolResult" and block.get("toolResult"):
                converse_content.append({"toolResult": _strip_media_from_tool_result(block["toolResult"])})
            elif block_type == "document" and block.get("document"):
                name = block["document"].get("name") if isinstance(block["document"], dict) else None
                if name:
                    block_names.append(str(name))
                else:
                    unnamed_attachments += 1
            elif block_type == "image" and block.get("image"):
                unnamed_attachments += 1
            elif block_type == "reasoningContent" and block.get("reasoningContent"):
                converse_content.append({"reasoningContent": block["reasoningContent"]})
            # Skip unknown/empty blocks

        if use_display_text and text_at is None:
            text_at = 0
            converse_content.insert(0, {"text": display_text})

        # The marker names every attachment (inline and diverted, unsanitized),
        # so the blocks' own names are only a fallback for a message without one.
        names = list(dict.fromkeys(marker_names or block_names))
        if not names and unnamed_attachments:
            names = [f"{unnamed_attachments} file(s)"]
        if names:
            note = {"text": _attachments_not_copied_note(names)}
            converse_content.insert(text_at + 1 if text_at is not None else len(converse_content), note)

        if not converse_content:
            return None

        return {"role": role, "content": converse_content}

    @staticmethod
    def _convert_floats_to_decimal(obj: Any) -> Any:
        """Recursively convert float values to Decimal for DynamoDB compatibility.

        DynamoDB's boto3 resource doesn't accept Python floats directly.
        This converts all floats in nested dicts/lists to Decimal.
        """
        if isinstance(obj, float):
            # Use string conversion to preserve precision
            return Decimal(str(obj))
        elif isinstance(obj, dict):
            return {k: ShareService._convert_floats_to_decimal(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [ShareService._convert_floats_to_decimal(item) for item in obj]
        return obj

    @staticmethod
    def _convert_decimals_to_float(obj: Any) -> Any:
        return convert_decimals_to_float(obj)

    @staticmethod
    def _sanitize_id(value: str, max_length: int = 128) -> str:
        """Return a log-safe version of an ID string.

        Strips anything that isn't an alphanumeric character or a hyphen/underscore,
        then truncates to ``max_length``.  This prevents log-injection attacks where
        a crafted ID embeds newlines or ANSI escape sequences.
        """
        sanitized = re.sub(r"[^a-zA-Z0-9\-_]", "", value)
        return sanitized[:max_length]

    # ------------------------------------------------------------------
    # Project shares

    def _projects(self) -> ProjectRepository:
        if self._project_repository is None:
            self._project_repository = ProjectRepository()
        return self._project_repository

    def _project_role(self, project_id: str, user: User):
        """``(project, role)`` for ``user``; ``(None, None)`` while Projects is switched off."""
        if not projects_enabled():
            return None, None
        return resolve_project_role(project_id, user.user_id, user.email, repository=self._projects())

    def _require_shareable_project(self, metadata: Any, user: User) -> Project:
        """The project a task may be shared to, or a ``ProjectShareError`` saying why not.

        The task must belong to a project (``preferences.projectId``), and its owner
        must still be a member of that project, which must be active: sharing adds
        to the project, and an archived project is read-only.
        """
        if not projects_enabled():
            raise ProjectShareError(400, "Projects are not available")
        prefs = getattr(metadata, "preferences", None)
        project_id = getattr(prefs, "project_id", None) if prefs else None
        if not project_id:
            raise ProjectShareError(400, "Only a task in a project can be shared with a project")
        project, role = self._project_role(project_id, user)
        if project is None or role is None:
            raise ProjectShareError(403, "You are not a member of this task's project")
        if project.status != "active":
            raise ProjectShareError(409, "This project is archived. Restore it to share tasks with it.")
        return project

    def _notify_recipients(self, project: Project, request: CreateShareRequest, user: User) -> List[str]:
        """The inboxes a project share's ``notify`` reaches, never including the sharer.

        Members means the owner plus every member row, pending invitees included.
        Naming anyone else is a 400 that names them, so a typo is caught rather
        than silently notifying nobody.
        """
        if request.notify is None:
            return []
        members = {normalize_email(m.email) for m in self._projects().list_members(project.project_id)}
        members.add(normalize_email(project.owner_email))
        if request.notify.all:
            chosen = members
        else:
            chosen = {normalize_email(e) for e in request.notify.emails or []} - {""}
            strangers = sorted(chosen - members)
            if strangers:
                raise ProjectShareError(400, f"Not members of this project: {', '.join(strangers)}")
        chosen.discard(normalize_email(user.email))
        return sorted(chosen)[: max_members()]

    def _notify_task_shared(self, recipients: List[str], user: User, project: Project, item: dict) -> None:
        payload = {"shareId": item["share_id"], "title": self._share_title(item)}
        if item.get("note"):
            payload["note"] = item["note"]
        notifications = self._notifications or NotificationService()
        notifications.notify_many(
            recipients,
            kind="project_task_shared",
            actor=user,
            project_id=project.project_id,
            project_name=project.name,
            payload=payload,
        )

    def _record_task_share(self, action: str, user: User, item: dict, **details: Any) -> None:
        """Audit a task entering or leaving a project, on the project's trail.

        ``details`` add to ``after``: a share records ``notified`` (a count, never
        the list) and ``hasNote``.
        """
        (self._audit or get_audit_service()).record(
            action=action,
            actor=user,
            target_type=TARGET_PROJECT,
            target_id=item["project_id"],
            after={"shareId": item["share_id"], "title": self._share_title(item), **details},
        )

    def _put_project_pointer(self, item: dict) -> None:
        self._projects().put_shared_task(
            SharedTask(
                project_id=item["project_id"],
                session_id=item["session_id"],
                share_id=item["share_id"],
                owner_id=item["owner_id"],
                owner_email=item.get("owner_email", ""),
                title=self._share_title(item),
                shared_at=item["created_at"],
                note=item.get("note"),
            )
        )

    def _sync_project_pointer(self, session_id: str, project_id: str) -> None:
        """Point ``SHARED_TASK#{session_id}`` at the task's newest share with the project.

        Rebuilt from the share rows rather than patched, so revoking the newest of
        several project shares falls back to the next, and revoking the last one
        removes the pointer. Best-effort: the share row is the grant and has
        already changed; a stale pointer only lists a share that now 404s.
        """
        try:
            remaining = [
                i for i in self._find_shares_by_session(session_id)
                if i.get("access_level") == "project" and i.get("project_id") == project_id
            ]
            if remaining:
                self._put_project_pointer(max(remaining, key=lambda i: i["created_at"]))
            else:
                self._projects().delete_shared_task(project_id, session_id)
        except Exception:
            logger.warning(
                f"Could not sync project pointer for session {self._sanitize_id(session_id)} "
                f"in project {self._sanitize_id(project_id)}",
                exc_info=True,
            )

    def _share_title(self, item: dict) -> str:
        """The row's title; shares created before it was denormalized read the snapshot."""
        if item.get("title") is not None:
            return str(item["title"])
        try:
            metadata, _ = self._load_snapshot_body(item)
            return str(metadata.get("title") or "")
        except Exception:
            return ""

    def _forked_project_preferences(self, snapshot_metadata: dict, requester: User):
        """A fork stays in its project when the requester can work in that project.

        The fork is a new task, so it needs what a task started in the project
        has: ``projectId`` and the project's harness as ``assistantId`` (current,
        not the snapshot's, which could be stale). A requester who is not a
        member, or a project that is archived or gone, gets a plain session.
        """
        from apis.shared.sessions.models import SessionPreferences

        prefs = snapshot_metadata.get("preferences") or {}
        project_id = prefs.get("projectId") if isinstance(prefs, dict) else None
        if not project_id:
            return None
        project, role = self._project_role(project_id, requester)
        if project is None or role is None or project.status != "active":
            return None
        return SessionPreferences(project_id=project_id, assistant_id=project.harness_agent_id)

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise ShareTableNotFoundError()

    def _get_share_item(self, share_id: str) -> Optional[dict]:
        try:
            resp = self._table.get_item(Key={"share_id": share_id})
            return resp.get("Item")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.error(f"Shared conversations table '{self._table_name}' not found - has CDK been deployed?")
                raise ShareTableNotFoundError()
            raise

    def _find_shares_by_session(self, session_id: str) -> List[dict]:
        """Return all shares for a given session_id."""
        try:
            resp = self._table.query(
                IndexName="SessionShareIndex",
                KeyConditionExpression=Key("session_id").eq(session_id),
            )
            return resp.get("Items", [])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.error(f"Shared conversations table '{self._table_name}' not found - has CDK been deployed?")
                raise ShareTableNotFoundError()
            raise

    @staticmethod
    def _resolve_allowed_emails(
        access_level: str,
        allowed_emails: Optional[List[str]],
        owner_email: str,
    ) -> Optional[List[str]]:
        if access_level != "specific":
            return None
        emails = list(allowed_emails or [])
        if owner_email.lower() not in [e.lower() for e in emails]:
            emails.insert(0, owner_email)
        return emails

    def _check_access(self, item: dict, requester: User) -> None:
        access_level = item.get("access_level", "specific")

        # Owner always has access
        if requester.user_id == item["owner_id"]:
            return

        if access_level == "public":
            return

        if access_level == "specific":
            allowed = [e.lower() for e in item.get("allowed_emails", [])]
            if requester.email.lower() in allowed:
                return

        if access_level == "project" and item.get("project_id"):
            # Any role, on an active or archived project: members may read both.
            _, role = self._project_role(item["project_id"], requester)
            if role is not None:
                return

        raise AccessDeniedError()

    def _build_share_response(self, item: dict) -> ShareResponse:
        return ShareResponse(
            share_id=item["share_id"],
            session_id=item["session_id"],
            owner_id=item["owner_id"],
            access_level=item["access_level"],
            allowed_emails=item.get("allowed_emails"),
            project_id=item.get("project_id"),
            created_at=item["created_at"],
            share_url=f"/shared/{item['share_id']}",
        )

    def _load_snapshot_body(self, item: dict) -> Tuple[dict, list]:
        """Return ``(metadata, messages)`` for a share item.

        A narrowing of :meth:`_load_snapshot_raw` kept because it is what
        every existing caller wants; anything needing another key of the
        body (the pinned artifact list) reads the raw dict instead.
        """
        body = self._load_snapshot_raw(item)
        return body.get("metadata", {}) or {}, body.get("messages", []) or []

    def _load_snapshot_raw(self, item: dict) -> dict:
        """Return the whole snapshot body for a share item (``load_snapshot_raw``).

        An unreadable body is a ``ShareNotFoundError``: a share whose snapshot
        is gone cannot be shown.
        """
        try:
            return load_snapshot_raw(item, self._snapshot_store)
        except SnapshotUnreadableError as e:
            logger.error(
                f"Failed to load snapshot body for share "
                f"{self._sanitize_id(str(item.get('share_id', '')))}: {e}"
            )
            raise ShareNotFoundError() from e

    def _delete_snapshot_body(self, item: dict) -> None:
        """Best-effort delete of a share's S3 snapshot body.

        No-op for legacy inline shares (no ``body_ref``). Never raises — the
        store's delete swallows storage misses; a failure here logs but never
        blocks the DynamoDB delete that actually revokes the share.
        """
        body_ref = item.get("body_ref")
        if not body_ref:
            return
        key = body_ref.get("bucket_key")
        if key:
            self._snapshot_store.delete(key)

    @staticmethod
    def _snapshot_artifacts(session_id: str, user: User) -> list[dict]:
        """The session's artifacts at HEAD, for the snapshot body.

        Best-effort and never raising. Sharing a conversation must not
        fail because the artifacts feature is off in this environment,
        or because its table hiccuped — a share with no artifacts is the
        behaviour every share had before this existed, and it degrades
        to exactly that. The alternative, failing the share, would trade
        a missing picture for a missing conversation.
        """
        try:
            from apis.app_api.artifacts.service import (
                get_artifact_list_service,
            )

            return get_artifact_list_service().heads_for_session(
                user_id=user.user_id, session_id=session_id
            )
        except Exception:
            logger.warning(
                "could not snapshot artifacts for session %s — sharing "
                "the conversation without them",
                ShareService._sanitize_id(session_id),
                exc_info=True,
            )
            return []

    def resolve_shared_artifact(
        self, *, share_id: str, artifact_id: str, requester: User
    ) -> tuple[str, int]:
        """Authorize one artifact inside a shared conversation.

        Returns (owner_id, pinned_version) for a caller that is about to
        mint. Raises ShareNotFoundError when the share is gone or does
        not carry that artifact, and AccessDeniedError when the viewer
        may not open the share.

        ############################################################
        # This is the access-control boundary for artifacts in shared
        # conversations, and it is the whole of it — the mint it feeds
        # (`mint_for_conversation_share`) performs no checks of its
        # own, by design and by the comment on it.
        #
        # Two things have to hold, and both are here:
        #   1. the viewer may open this conversation share, and
        #   2. the artifact is one the SNAPSHOT pinned.
        #
        # (2) is what stops a recipient swapping in another artifact id
        # belonging to the same owner. `sub` on the minted token is a
        # partition address, so without it any valid share id would be
        # a read primitive over the owner's whole artifact partition.
        # An unknown artifact is a 404 rather than a 403, so it also
        # reveals nothing about what the owner has.
        ############################################################
        """
        self._ensure_enabled()

        item = self._get_share_item(share_id)
        if not item:
            raise ShareNotFoundError()

        self._check_access(item, requester)

        for entry in self._load_snapshot_artifacts(item):
            if str(entry.get("artifact_id", "")) == artifact_id:
                return str(item["owner_id"]), int(entry.get("version", 0))

        raise ShareNotFoundError()

    def _load_snapshot_artifacts(self, item: dict, body: Optional[dict] = None) -> list[dict]:
        """The pinned artifact list from a share's snapshot body.

        Absent on every share created before this feature, and on any
        share whose owner had no artifacts — both are a normal empty
        list, not an error. Conversation sharing is already in
        production, so this MUST stay tolerant of a body with no
        `artifacts` key; there is no migration and none is needed.
        """
        if body is None:
            try:
                body = self._load_snapshot_raw(item)
            except ShareNotFoundError:
                raise
            except Exception:
                logger.warning(
                    "could not read snapshot artifacts for share %s",
                    self._sanitize_id(str(item.get("share_id", ""))),
                    exc_info=True,
                )
                return []
        raw = body.get("artifacts")
        return raw if isinstance(raw, list) else []

    def _build_shared_conversation_response(self, item: dict) -> SharedConversationResponse:
        from apis.shared.sessions.models import MessageResponse

        # One read serves the messages, the artifacts and the summaries.
        body = self._load_snapshot_raw(item)
        metadata = body.get("metadata", {}) or {}
        raw_messages = body.get("messages", []) or []

        messages = []
        for msg_data in raw_messages:
            try:
                messages.append(MessageResponse.model_validate(msg_data))
            except Exception as e:
                logger.warning(f"Skipping malformed message in share {item['share_id']}: {e}")

        artifacts = []
        for entry in self._load_snapshot_artifacts(item, body):
            try:
                artifacts.append(
                    SharedConversationArtifact.model_validate(entry)
                )
            except Exception as e:
                # One malformed entry must not cost the recipient the
                # conversation, the same way a malformed message does not.
                logger.warning(
                    f"Skipping malformed artifact in share "
                    f"{self._sanitize_id(str(item.get('share_id', '')))}: {e}"
                )

        return SharedConversationResponse(
            share_id=item["share_id"],
            title=metadata.get("title", "Untitled Conversation"),
            access_level=item["access_level"],
            created_at=item["created_at"],
            owner_id=item["owner_id"],
            messages=messages,
            artifacts=artifacts,
            tool_summaries=self._snapshot_tool_summaries(body),
        )

    @staticmethod
    def _snapshot_tool_summaries(body: dict) -> list[dict]:
        """The summaries frozen with the snapshot; ``[]`` on shares made before they were."""
        raw = body.get("toolSummaries")
        if not isinstance(raw, list):
            return []
        return [
            {
                "batchId": str(row.get("batchId") or ""),
                "toolUseIds": [str(t) for t in row.get("toolUseIds") or []],
                "summary": str(row["summary"]),
            }
            for row in raw
            if isinstance(row, dict) and row.get("summary")
        ]


# ------------------------------------------------------------------
# Domain exceptions
# ------------------------------------------------------------------

class SessionNotFoundError(Exception):
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class ShareNotFoundError(Exception):
    pass


class NotOwnerError(Exception):
    pass


class AccessDeniedError(Exception):
    pass


class ShareTableNotFoundError(Exception):
    """Raised when the DynamoDB table does not exist (CDK not deployed)."""
    pass


class ProjectShareError(Exception):
    """A project share the caller may not create; carries the HTTP status to return."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class ShareStorageUnavailableError(Exception):
    """Raised when the S3 snapshot-body store is unconfigured or unreachable.

    The share body is offloaded to S3; if the bucket is unset (misconfigured
    deploy / local dev without AWS) or the write fails, creating a share can't
    proceed. Surfaced to the client as a 503 with a friendly message rather
    than silently falling back to inline (which would reintroduce the 400 KB
    item-size failure).
    """
    pass


# Global service instance (singleton)
_service_instance: Optional[ShareService] = None


def get_share_service() -> ShareService:
    """Get or create the global ShareService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = ShareService()
    return _service_instance
