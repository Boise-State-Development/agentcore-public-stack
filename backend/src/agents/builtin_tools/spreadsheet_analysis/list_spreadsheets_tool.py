"""List available spreadsheet files for analysis.

Factory function creates a context-bound tool that only exposes CSV/XLSX
files belonging to the current assistant's knowledge base or chat session.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from strands import tool

from apis.shared.files.models import is_tabular_file

logger = logging.getLogger(__name__)


def _is_tabular_file(filename: str, content_type: str) -> bool:
    """Deprecated wrapper — use apis.shared.files.models.is_tabular_file.

    Kept as a module-local name so existing callers in this file stay
    readable; shares the canonical implementation that the inference-api
    route uses when partitioning chat attachments (#206).
    """
    return is_tabular_file(filename, content_type)


def make_list_spreadsheets_tool(
    assistant_id: Optional[str],
    session_id: str,
    user_id: str,
):
    """Create a list_spreadsheets tool bound to the given context."""

    @tool
    async def list_spreadsheets() -> Dict[str, Any]:
        """List CSV/XLSX spreadsheet files available for analysis.

        Returns spreadsheets from the assistant's knowledge base (if a
        conversation is scoped to an assistant) and/or files attached to
        the current conversation. Use this to discover which files can be
        analyzed with the analyze_spreadsheet tool.

        Returns:
            Dictionary with 'files' list containing available spreadsheets,
            each with filename, source, content_type, size_bytes, and document_id.
        """
        files: List[Dict[str, Any]] = []

        # 1. Assistant KB files
        if assistant_id:
            files.extend(await _get_kb_files(assistant_id))

        # 2. Session-attached files
        files.extend(await _get_session_files(session_id))

        if not files:
            return {
                "content": [{"text": "No spreadsheet files (CSV or XLSX) are available. Upload a spreadsheet to the assistant's knowledge base or attach one to this conversation."}],
                "status": "success",
            }

        file_list = "\n".join(
            f"- {f['filename']} ({f['source']}, {f['size_bytes'] / 1024:.0f} KB)"
            for f in files
        )
        return {
            "content": [{"text": f"Available spreadsheet files:\n{file_list}"}],
            "status": "success",
            "files": files,
        }

    return list_spreadsheets


async def _get_kb_files(assistant_id: str) -> List[Dict[str, Any]]:
    """Query DynamoDB for completed tabular documents in the assistant's KB.

    The boto3 query is offloaded to a worker thread via ``asyncio.to_thread``
    so the event loop stays free to schedule other coroutines while the
    DynamoDB round-trip is in flight. Previously this was a sync function
    called from async contexts — see #260 for the regression it caused
    under concurrent chat load.
    """
    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        logger.warning("DYNAMODB_ASSISTANTS_TABLE_NAME not set, skipping KB files")
        return []

    def _query() -> Dict[str, Any]:
        dynamodb = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        table = dynamodb.Table(table_name)
        return table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={":pk": f"AST#{assistant_id}", ":sk_prefix": "DOC#"},
        )

    try:
        response = await asyncio.to_thread(_query)

        files = []
        for item in response.get("Items", []):
            if item.get("status") != "complete":
                continue
            filename = item.get("filename", "")
            content_type = item.get("contentType", item.get("content_type", ""))
            if not _is_tabular_file(filename, content_type):
                continue
            files.append({
                "filename": filename,
                "source": "knowledge_base",
                "content_type": content_type,
                "size_bytes": int(item.get("sizeBytes", item.get("size_bytes", 0))),
                "document_id": item.get("documentId", item.get("document_id", "")),
                "s3_key": item.get("s3Key", item.get("s3_key", "")),
            })
        return files

    except Exception as e:
        logger.error(f"Error querying KB files for assistant {assistant_id}: {e}")
        return []


async def _get_session_files(session_id: str) -> List[Dict[str, Any]]:
    """Query DynamoDB for tabular files attached to the current session.

    Awaits ``FileUploadRepository.list_session_files`` directly — replaces
    the earlier sync-in-async executor dance which spun up a thread pool
    per call and ran ``asyncio.run`` inside it. See #260.
    """
    try:
        from apis.shared.files.repository import get_file_upload_repository

        repo = get_file_upload_repository()
        session_files = await repo.list_session_files(session_id)

        files = []
        for f in session_files:
            if not _is_tabular_file(f.filename, f.mime_type):
                continue
            files.append({
                "filename": f.filename,
                "source": "chat_attachment",
                "content_type": f.mime_type,
                "size_bytes": f.size_bytes,
                "document_id": f.upload_id,
                "s3_key": f.s3_key,
                "s3_bucket": f.s3_bucket,
            })
        return files

    except Exception as e:
        logger.error(f"Error querying session files for {session_id}: {e}")
        return []
