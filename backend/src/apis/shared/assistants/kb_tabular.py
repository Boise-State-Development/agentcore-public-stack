"""Whether an assistant's knowledge base holds a spreadsheet.

The knowledge-base half of the Spreadsheet Analysis auto-enable. CSV/XLSX in a
knowledge base are never retrieved as text the model can compute over, so an
assistant whose knowledge base holds one needs ``list_spreadsheets`` /
``analyze_spreadsheet`` to use it at all. For an Agent that binds no tools those
used to arrive only through the viewer's own picker, which an unbound Agent no
longer reads (agent_binding_resolver: "An Agent's tools are the Agent's"), so the
route enables them from the knowledge base instead — the same way a spreadsheet
attachment enables them for the session.

Reads the same ``DOC#`` rows, with the same "complete" and tabular tests, that
``list_spreadsheets`` lists from (``agents/builtin_tools/spreadsheet_analysis``),
so the tools appear exactly when that listing would have something in it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

from apis.shared.files.models import is_tabular_file


def _has_tabular_document(assistant_id: str) -> bool:
    """Blocking: page the assistant's ``DOC#`` rows until a complete spreadsheet turns up."""
    from boto3.dynamodb.conditions import Key

    from apis.shared.aws_clients import get_dynamodb_table

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        return False
    table = get_dynamodb_table(table_name, os.environ.get("AWS_REGION", "us-west-2"))
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("DOC#"),
        # Only what the test below reads; a knowledge base's rows also carry chunk and
        # ingestion metadata that a presence check has no use for.
        "ProjectionExpression": "#status, filename, contentType, content_type",
        "ExpressionAttributeNames": {"#status": "status"},
    }
    while True:
        response = table.query(**query)
        for item in response.get("Items", []):
            if item.get("status") != "complete":
                continue
            content_type = item.get("contentType", item.get("content_type", "")) or ""
            if is_tabular_file(item.get("filename", "") or "", content_type):
                return True
        last = response.get("LastEvaluatedKey")
        if not last:
            return False
        query["ExclusiveStartKey"] = last


async def assistant_kb_has_tabular_documents(assistant_id: str) -> bool:
    """Whether ``assistant_id``'s knowledge base has at least one complete CSV/XLSX.

    One DynamoDB Query (paged only for a very large knowledge base), off the event
    loop. Raises on a read failure; the caller decides how to fail.
    """
    if not assistant_id:
        return False
    return await asyncio.to_thread(_has_tabular_document, assistant_id)
