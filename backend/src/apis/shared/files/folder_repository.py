"""DynamoDB access for folder attachment rows (drive-folder spec §4.2).

Same table as the file rows (``repository.py``), under the ``FOLDER#`` sort
prefix on both the table and the ``SessionIndex`` GSI, so a session's folders
and files share a partition and the file listings exclude folders by prefix.
boto3 is synchronous; the ``async`` methods are thin wrappers, as the file
repository's are.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from apis.shared.aws_clients import get_dynamodb_table
from apis.shared.files.folder_attachments import (
    RECENT_FOLDERS_MAX_ROWS,
    FolderAttachment,
    FolderCounts,
    FolderFileEntry,
    RecentFolder,
    RefreshSummary,
)
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

_KEYS = {"PK", "SK", "GSI1PK", "GSI1SK"}


class FolderAttachmentRepository:
    """Create, read, update, delete and list-by-session for folder rows."""

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get("DYNAMODB_USER_FILES_TABLE_NAME", "user-files")
        # The process-cached resource: the Runtime reads folder rows on the
        # turn path, where constructing a resource is ~47 ms the first time.
        self._table = get_dynamodb_table(self.table_name)

    @staticmethod
    def _key(user_id: str, folder_attach_id: str) -> Dict[str, str]:
        return {"PK": f"USER#{user_id}", "SK": f"FOLDER#{folder_attach_id}"}

    async def create(self, folder: FolderAttachment) -> FolderAttachment:
        try:
            self._table.put_item(
                Item=folder.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
            return folder
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Folder attachment '{folder.folder_attach_id}' already exists")
            logger.error("Error creating folder attachment row: %s", e)
            raise

    async def get(self, user_id: str, folder_attach_id: str) -> Optional[FolderAttachment]:
        return self.get_sync(user_id, folder_attach_id)

    def get_sync(self, user_id: str, folder_attach_id: str) -> Optional[FolderAttachment]:
        """One ``GetItem``. Synchronous so the turn path can run it in a worker
        thread (``asyncio.to_thread``) instead of blocking the event loop."""
        try:
            response = self._table.get_item(Key=self._key(user_id, folder_attach_id))
        except ClientError as e:
            logger.error("Error reading folder attachment row: %s", e)
            raise
        item = response.get("Item")
        return FolderAttachment.from_dynamo_item(item) if item else None

    async def update(
        self,
        user_id: str,
        folder_attach_id: str,
        *,
        status: Optional[str] = None,
        counts: Optional[FolderCounts] = None,
        files: Optional[List[FolderFileEntry]] = None,
        error: Optional[str] = None,
        clear_error: bool = False,
        snapshot_at: Optional[str] = None,
        refreshing: Optional[bool] = None,
        refreshed_at: Optional[str] = None,
        last_refresh: Optional["RefreshSummary"] = None,
    ) -> Optional[FolderAttachment]:
        """Set the given fields and ``updatedAt`` on an existing row.

        Returns the row as updated, or ``None`` when it no longer exists (the
        user removed the folder while its task was running: the task's next
        write finds nothing and stops).
        """
        sets: List[str] = ["updatedAt = :updated_at"]
        removes: List[str] = []
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {":updated_at": utc_now_iso()}
        if status is not None:
            names["#status"] = "status"
            sets.append("#status = :status")
            values[":status"] = status
        if counts is not None:
            sets.append("counts = :counts")
            values[":counts"] = counts.model_dump(by_alias=True)
        if files is not None:
            sets.append("files = :files")
            values[":files"] = [f.model_dump(by_alias=True, exclude_none=True) for f in files]
        if snapshot_at is not None:
            sets.append("snapshotAt = :snapshot_at")
            values[":snapshot_at"] = snapshot_at
        if refreshing is not None:
            sets.append("refreshing = :refreshing")
            values[":refreshing"] = refreshing
        if refreshed_at is not None:
            sets.append("refreshedAt = :refreshed_at")
            values[":refreshed_at"] = refreshed_at
        if last_refresh is not None:
            sets.append("lastRefresh = :last_refresh")
            values[":last_refresh"] = last_refresh.model_dump(by_alias=True)
        if error is not None:
            names["#error"] = "error"
            sets.append("#error = :error")
            values[":error"] = error
        elif clear_error:
            names["#error"] = "error"
            removes.append("#error")

        expression = "SET " + ", ".join(sets)
        if removes:
            expression += " REMOVE " + ", ".join(removes)
        params: Dict[str, Any] = {
            "Key": self._key(user_id, folder_attach_id),
            "UpdateExpression": expression,
            "ExpressionAttributeValues": values,
            "ConditionExpression": "attribute_exists(PK)",
            "ReturnValues": "ALL_NEW",
        }
        if names:
            params["ExpressionAttributeNames"] = names
        try:
            response = self._table.update_item(**params)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            logger.error("Error updating folder attachment row: %s", e)
            raise
        return FolderAttachment.from_dynamo_item(response["Attributes"])

    async def delete(self, user_id: str, folder_attach_id: str) -> Optional[FolderAttachment]:
        try:
            response = self._table.delete_item(Key=self._key(user_id, folder_attach_id), ReturnValues="ALL_OLD")
        except ClientError as e:
            logger.error("Error deleting folder attachment row: %s", e)
            raise
        old = response.get("Attributes")
        return FolderAttachment.from_dynamo_item(old) if old else None

    async def list_session_folders(self, session_id: str) -> List[FolderAttachment]:
        """Every folder row of a session, newest first (the ``SessionIndex`` GSI)."""
        try:
            params: Dict[str, Any] = {
                "IndexName": "SessionIndex",
                "KeyConditionExpression": "GSI1PK = :pk AND begins_with(GSI1SK, :sk_prefix)",
                "ExpressionAttributeValues": {":pk": f"CONV#{session_id}", ":sk_prefix": "FOLDER#"},
                "ScanIndexForward": False,
            }
            response = self._table.query(**params)
            items = list(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self._table.query(**params)
                items.extend(response.get("Items", []))
        except ClientError as e:
            logger.error("Error listing folder attachments for a session: %s", e)
            raise
        return [FolderAttachment.from_dynamo_item(item) for item in items]

    #: Rows per page of the recent-folders query: most users fill the list
    #: from the first page.
    _RECENT_PAGE_SIZE = 25

    async def list_recent_folders(
        self, user_id: str, limit: int, *, max_rows: int = RECENT_FOLDERS_MAX_ROWS
    ) -> List[RecentFolder]:
        """The user's most recently attached source folders, one per folder.

        A key query on the user's own partition (``PK=USER#{user}``, the
        ``FOLDER#`` sort prefix). Folder ids lead with a millisecond timestamp,
        so a descending read is newest first. Only the few attributes the
        picker shows are projected, never the file list, and the read stops at
        ``limit`` distinct folders or ``max_rows`` rows, whichever comes first.
        A conversation's rows go when it is deleted, so its folders drop out.
        """
        params: Dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
            "ExpressionAttributeValues": {":pk": f"USER#{user_id}", ":sk_prefix": "FOLDER#"},
            "ProjectionExpression": "connectorId, connectorName, providerFolderId, folderName, folderPath, createdAt",
            "ScanIndexForward": False,
            "Limit": min(self._RECENT_PAGE_SIZE, max_rows),
        }
        recent: List[RecentFolder] = []
        seen: set = set()
        read = 0
        try:
            while True:
                response = self._table.query(**params)
                for item in response.get("Items", []):
                    read += 1
                    key = (item.get("connectorId"), item.get("providerFolderId"))
                    if None in key or key in seen:
                        continue
                    seen.add(key)
                    recent.append(
                        RecentFolder(
                            connector_id=key[0],
                            connector_name=item.get("connectorName"),
                            provider_folder_id=key[1],
                            folder_name=item.get("folderName") or "Folder",
                            folder_path=item.get("folderPath"),
                            last_attached_at=item.get("createdAt") or "",
                        )
                    )
                    if len(recent) >= limit:
                        return recent
                if read >= max_rows or "LastEvaluatedKey" not in response:
                    return recent
                params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                params["Limit"] = min(self._RECENT_PAGE_SIZE, max_rows - read)
        except ClientError as e:
            logger.error("Error listing recent folder attachments: %s", e)
            raise

    async def delete_session_folders(self, session_id: str) -> List[FolderAttachment]:
        """Delete a session's folder rows (the file rows go through the file cascade)."""
        deleted: List[FolderAttachment] = []
        for folder in await self.list_session_folders(session_id):
            try:
                old = await self.delete(folder.user_id, folder.folder_attach_id)
            except Exception as e:  # noqa: BLE001 - best-effort cascade
                logger.warning("Failed to delete a folder attachment row in a session cascade: %s", e)
                continue
            if old:
                deleted.append(old)
        return deleted


_repository_instance: Optional[FolderAttachmentRepository] = None


def get_folder_attachment_repository() -> FolderAttachmentRepository:
    """Get or create the process-wide repository."""
    global _repository_instance
    if _repository_instance is None:
        _repository_instance = FolderAttachmentRepository()
    return _repository_instance
