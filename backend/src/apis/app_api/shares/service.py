"""Share service layer

Business logic for creating, retrieving, updating, and revoking
conversation share snapshots.  Supports multiple shares per session.
"""

import logging
import os
import re
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from apis.shared.auth.models import User
from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.metadata import get_session_metadata, store_session_metadata

from .models import (
    CreateShareRequest,
    ShareListResponse,
    ShareResponse,
    SharedConversationResponse,
    UpdateShareRequest,
)

logger = logging.getLogger(__name__)


class ShareService:
    """Handles share CRUD operations against the shared-conversations DynamoDB table."""

    def __init__(self) -> None:
        table_name = os.environ.get("SHARED_CONVERSATIONS_TABLE_NAME", "")
        self._table_name = table_name
        self._enabled = bool(table_name)

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
    ) -> ShareResponse:
        """Create a new share snapshot for a session.

        Multiple shares can exist per session (e.g. after continuing a conversation).
        """
        self._ensure_enabled()

        # Verify session ownership
        metadata = await get_session_metadata(session_id=session_id, user_id=user.user_id)
        if not metadata:
            raise SessionNotFoundError(session_id)

        # Snapshot messages
        messages_response = await get_messages(session_id=session_id, user_id=user.user_id)
        messages_snapshot = [
            msg.model_dump(by_alias=True, exclude_none=True)
            for msg in messages_response.messages
        ]

        metadata_snapshot = metadata.model_dump(by_alias=True, exclude_none=True)

        # Convert floats to Decimal for DynamoDB compatibility
        messages_snapshot = self._convert_floats_to_decimal(messages_snapshot)
        metadata_snapshot = self._convert_floats_to_decimal(metadata_snapshot)

        # Build item
        share_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        allowed_emails = self._resolve_allowed_emails(
            request.access_level, request.allowed_emails, user.email
        )

        item = {
            "share_id": share_id,
            "session_id": session_id,
            "owner_id": user.user_id,
            "owner_email": user.email,
            "access_level": request.access_level,
            "created_at": now,
            "metadata": metadata_snapshot,
            "messages": messages_snapshot,
        }
        if allowed_emails is not None:
            item["allowed_emails"] = allowed_emails

        self._table.put_item(Item=item)
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

        new_access = request.access_level or item.get("access_level")

        if request.access_level is not None:
            update_expr_parts.append("access_level = :al")
            attr_values[":al"] = request.access_level

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

        snapshot_messages = item.get("messages", [])
        metadata = item.get("metadata", {})
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
        persists via create_message to the "default" namespace.

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
            from strands.types.session import SessionMessage
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

        count = 0
        for idx, msg_dict in enumerate(snapshot_messages):
            converse_msg = self._snapshot_msg_to_converse(msg_dict)
            if converse_msg is None:
                continue
            try:
                # Create SessionMessage with proper index for ordering
                session_msg = SessionMessage.from_message(converse_msg, index=idx)
                # Use create_message with "default" namespace (same as list_messages uses)
                await asyncio.to_thread(mgr.create_message, session_id, "default", session_msg)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to copy message {idx}: {e}")

        logger.info(f"Copied {count}/{len(snapshot_messages)} messages to AgentCore Memory")
        return count

    @staticmethod
    def _snapshot_msg_to_converse(msg: dict) -> Optional[dict]:
        """Convert a snapshot MessageResponse dict to Bedrock Converse format.

        Snapshot format (MessageResponse):
            {"id": "...", "role": "user", "content": [{"type": "text", "text": "hi"}, ...], ...}

        Converse format (Strands/Bedrock):
            {"role": "user", "content": [{"text": "hi"}, ...]}
        """
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return None

        raw_content = msg.get("content", [])
        converse_content = []

        for block in raw_content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text" and block.get("text"):
                converse_content.append({"text": block["text"]})
            elif block_type == "toolUse" and block.get("toolUse"):
                converse_content.append({"toolUse": block["toolUse"]})
            elif block_type == "toolResult" and block.get("toolResult"):
                converse_content.append({"toolResult": block["toolResult"]})
            elif block_type == "image" and block.get("image"):
                converse_content.append({"image": block["image"]})
            elif block_type == "document" and block.get("document"):
                converse_content.append({"document": block["document"]})
            elif block_type == "reasoningContent" and block.get("reasoningContent"):
                converse_content.append({"reasoningContent": block["reasoningContent"]})
            # Skip unknown/empty blocks

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
    def _sanitize_id(value: str, max_length: int = 128) -> str:
        """Return a log-safe version of an ID string.

        Strips anything that isn't an alphanumeric character or a hyphen/underscore,
        then truncates to ``max_length``.  This prevents log-injection attacks where
        a crafted ID embeds newlines or ANSI escape sequences.
        """
        sanitized = re.sub(r"[^a-zA-Z0-9\-_]", "", value)
        return sanitized[:max_length]

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

        raise AccessDeniedError()

    def _build_share_response(self, item: dict) -> ShareResponse:
        return ShareResponse(
            share_id=item["share_id"],
            session_id=item["session_id"],
            owner_id=item["owner_id"],
            access_level=item["access_level"],
            allowed_emails=item.get("allowed_emails"),
            created_at=item["created_at"],
            share_url=f"/shared/{item['share_id']}",
        )

    def _build_shared_conversation_response(self, item: dict) -> SharedConversationResponse:
        from apis.shared.sessions.models import MessageResponse

        metadata = item.get("metadata", {})
        raw_messages = item.get("messages", [])

        messages = []
        for msg_data in raw_messages:
            try:
                messages.append(MessageResponse.model_validate(msg_data))
            except Exception as e:
                logger.warning(f"Skipping malformed message in share {item['share_id']}: {e}")

        return SharedConversationResponse(
            share_id=item["share_id"],
            title=metadata.get("title", "Untitled Conversation"),
            access_level=item["access_level"],
            created_at=item["created_at"],
            owner_id=item["owner_id"],
            messages=messages,
        )


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


# Global service instance (singleton)
_service_instance: Optional[ShareService] = None


def get_share_service() -> ShareService:
    """Get or create the global ShareService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = ShareService()
    return _service_instance
