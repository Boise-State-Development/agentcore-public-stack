"""
File Upload Repository

DynamoDB operations for file metadata and user quota tracking.
"""

import os
import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from .models import FileMetadata, UserFileQuota, FileStatus

logger = logging.getLogger(__name__)


class FileUploadRepository:
    """
    Repository for File Upload CRUD operations in DynamoDB.

    Key Patterns:
    - File: PK=USER#{userId}, SK=FILE#{uploadId}
    - Quota: PK=USER#{userId}, SK=QUOTA

    GSI (SessionIndex):
    - GSI1PK=CONV#{sessionId}, GSI1SK=FILE#{uploadId}
    """

    def __init__(self, table_name: Optional[str] = None):
        """Initialize repository with DynamoDB table."""
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_USER_FILES_TABLE_NAME", "user-files"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    # =========================================================================
    # File CRUD Operations
    # =========================================================================

    async def create_file(self, file_meta: FileMetadata) -> FileMetadata:
        """
        Create a new file metadata record.

        Args:
            file_meta: The FileMetadata to create

        Returns:
            The created FileMetadata
        """
        try:
            item = file_meta.to_dynamo_item()
            # Convert floats to Decimals for DynamoDB
            item = self._convert_floats_to_decimals(item)

            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )

            logger.info(f"Created file metadata: {file_meta.upload_id}")
            return file_meta

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"File '{file_meta.upload_id}' already exists")
            logger.error(f"Error creating file metadata: {e}")
            raise

    async def get_file(self, user_id: str, upload_id: str) -> Optional[FileMetadata]:
        """
        Get a file by user ID and upload ID.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            FileMetadata if found, None otherwise
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": f"FILE#{upload_id}"}
            )
            item = response.get("Item")
            if not item:
                return None
            return FileMetadata.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting file {upload_id}: {e}")
            raise

    async def update_file_status(
        self, user_id: str, upload_id: str, status: FileStatus
    ) -> Optional[FileMetadata]:
        """
        Update a file's status.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier
            status: New status

        Returns:
            Updated FileMetadata or None if not found
        """
        try:
            response = self._table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"FILE#{upload_id}"},
                UpdateExpression="SET #status = :status, updatedAt = :now",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": status.value if isinstance(status, FileStatus) else status,
                    ":now": datetime.utcnow().isoformat() + "Z",
                },
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="ALL_NEW",
            )
            return FileMetadata.from_dynamo_item(response["Attributes"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            logger.error(f"Error updating file status {upload_id}: {e}")
            raise

    async def delete_file(self, user_id: str, upload_id: str) -> Optional[FileMetadata]:
        """
        Delete a file metadata record.

        Args:
            user_id: The owner's user ID
            upload_id: The upload identifier

        Returns:
            Deleted FileMetadata or None if not found
        """
        try:
            response = self._table.delete_item(
                Key={"PK": f"USER#{user_id}", "SK": f"FILE#{upload_id}"},
                ReturnValues="ALL_OLD",
            )
            old_item = response.get("Attributes")
            if not old_item:
                return None

            logger.info(f"Deleted file metadata: {upload_id}")
            return FileMetadata.from_dynamo_item(old_item)
        except ClientError as e:
            logger.error(f"Error deleting file {upload_id}: {e}")
            raise

    async def list_user_files(
        self,
        user_id: str,
        limit: int = 20,
        cursor: Optional[str] = None,
        status: Optional[FileStatus] = None,
    ) -> Tuple[List[FileMetadata], Optional[str]]:
        """
        List files for a user with pagination.

        Args:
            user_id: The user identifier
            limit: Maximum number of files to return
            cursor: Pagination cursor (last evaluated key as base64)
            status: Optional status filter

        Returns:
            Tuple of (files, next_cursor)
        """
        try:
            query_params = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
                "ExpressionAttributeValues": {
                    ":pk": f"USER#{user_id}",
                    ":sk_prefix": "FILE#",
                },
                "Limit": limit,
                "ScanIndexForward": False,  # Newest first (ULID sorts chronologically)
            }

            if status:
                query_params["FilterExpression"] = "#status = :status"
                query_params["ExpressionAttributeNames"] = {"#status": "status"}
                query_params["ExpressionAttributeValues"][":status"] = (
                    status.value if isinstance(status, FileStatus) else status
                )

            if cursor:
                import base64
                import json
                query_params["ExclusiveStartKey"] = json.loads(
                    base64.b64decode(cursor).decode("utf-8")
                )

            response = self._table.query(**query_params)

            files = [FileMetadata.from_dynamo_item(item) for item in response.get("Items", [])]

            # Build next cursor
            next_cursor = None
            if "LastEvaluatedKey" in response:
                import base64
                import json
                next_cursor = base64.b64encode(
                    json.dumps(response["LastEvaluatedKey"]).encode("utf-8")
                ).decode("utf-8")

            return files, next_cursor

        except ClientError as e:
            logger.error(f"Error listing files for user {user_id}: {e}")
            raise

    async def list_session_files(
        self, session_id: str, status: Optional[FileStatus] = None
    ) -> List[FileMetadata]:
        """
        List files for a session using the SessionIndex GSI.

        Args:
            session_id: The session/conversation identifier
            status: Optional status filter

        Returns:
            List of FileMetadata
        """
        try:
            query_params = {
                "IndexName": "SessionIndex",
                "KeyConditionExpression": "GSI1PK = :pk",
                "ExpressionAttributeValues": {":pk": f"CONV#{session_id}"},
                "ScanIndexForward": False,
            }

            if status:
                query_params["FilterExpression"] = "#status = :status"
                query_params["ExpressionAttributeNames"] = {"#status": "status"}
                query_params["ExpressionAttributeValues"][":status"] = (
                    status.value if isinstance(status, FileStatus) else status
                )

            response = self._table.query(**query_params)
            items = response.get("Items", [])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                query_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = self._table.query(**query_params)
                items.extend(response.get("Items", []))

            return [FileMetadata.from_dynamo_item(item) for item in items]

        except ClientError as e:
            logger.error(f"Error listing files for session {session_id}: {e}")
            raise

    # =========================================================================
    # Quota Operations
    # =========================================================================

    async def get_user_quota(self, user_id: str) -> UserFileQuota:
        """
        Get user's file quota.

        Args:
            user_id: The user identifier

        Returns:
            UserFileQuota (empty if not found)
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": "QUOTA"}
            )
            item = response.get("Item")
            if not item:
                return UserFileQuota(user_id=user_id)
            return UserFileQuota.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting quota for user {user_id}: {e}")
            raise

    async def increment_quota(self, user_id: str, size_bytes: int) -> UserFileQuota:
        """
        Atomically increment user's quota.

        Args:
            user_id: The user identifier
            size_bytes: Bytes to add

        Returns:
            Updated UserFileQuota
        """
        try:
            response = self._table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": "QUOTA"},
                UpdateExpression=(
                    "SET userId = :userId, updatedAt = :now "
                    "ADD totalBytes :size, fileCount :one"
                ),
                ExpressionAttributeValues={
                    ":userId": user_id,
                    ":now": datetime.utcnow().isoformat() + "Z",
                    ":size": size_bytes,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
            return UserFileQuota.from_dynamo_item(response["Attributes"])
        except ClientError as e:
            logger.error(f"Error incrementing quota for user {user_id}: {e}")
            raise

    async def decrement_quota(self, user_id: str, size_bytes: int) -> UserFileQuota:
        """
        Atomically decrement user's quota.

        Args:
            user_id: The user identifier
            size_bytes: Bytes to remove

        Returns:
            Updated UserFileQuota
        """
        try:
            response = self._table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": "QUOTA"},
                UpdateExpression=(
                    "SET userId = :userId, updatedAt = :now "
                    "ADD totalBytes :negSize, fileCount :negOne"
                ),
                ExpressionAttributeValues={
                    ":userId": user_id,
                    ":now": datetime.utcnow().isoformat() + "Z",
                    ":negSize": -size_bytes,
                    ":negOne": -1,
                },
                ReturnValues="ALL_NEW",
            )
            return UserFileQuota.from_dynamo_item(response["Attributes"])
        except ClientError as e:
            logger.error(f"Error decrementing quota for user {user_id}: {e}")
            raise

    # =========================================================================
    # Cascade Delete Operations
    # =========================================================================

    async def delete_session_files(self, session_id: str) -> list[FileMetadata]:
        """
        Delete all files for a session (cascade delete).

        Uses the SessionIndex GSI to find all files, then deletes each one.
        Also decrements the user quota for each deleted file.

        Args:
            session_id: The session/conversation identifier

        Returns:
            List of deleted FileMetadata records
        """
        deleted_files = []

        try:
            # Get all files for this session (including pending)
            files = await self.list_session_files(session_id, status=None)

            if not files:
                logger.info(f"No files found for session {session_id}")
                return deleted_files

            # Delete each file's metadata
            for file_meta in files:
                try:
                    deleted = await self.delete_file(file_meta.user_id, file_meta.upload_id)
                    if deleted:
                        deleted_files.append(deleted)
                        logger.debug(f"Deleted file {file_meta.upload_id} for session cascade delete")
                except Exception as e:
                    logger.warning(f"Failed to delete file {file_meta.upload_id}: {e}")

            logger.info(f"Cascade deleted {len(deleted_files)} files for session {session_id}")
            return deleted_files

        except ClientError as e:
            logger.error(f"Error in cascade delete for session {session_id}: {e}")
            raise

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _convert_floats_to_decimals(self, item: dict) -> dict:
        """Convert float values to Decimal for DynamoDB compatibility."""
        converted = {}
        for key, value in item.items():
            if isinstance(value, float):
                converted[key] = Decimal(str(value))
            elif isinstance(value, dict):
                converted[key] = self._convert_floats_to_decimals(value)
            else:
                converted[key] = value
        return converted


# Global repository instance
_repository_instance: Optional[FileUploadRepository] = None


def get_file_upload_repository() -> FileUploadRepository:
    """Get or create the global FileUploadRepository instance."""
    global _repository_instance
    if _repository_instance is None:
        _repository_instance = FileUploadRepository()
    return _repository_instance
