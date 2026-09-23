"""The harness gateway app-api injects: create as the default, delete with cleanup.

Purging a project must remove its harness the way ``DELETE /assistants`` removes
an agent — documents soft-deleted, sync policies removed, vectors and objects
cleaned up in the background — or the project's knowledge outlives it. That
cleanup lives in app-api, which ``apis.shared.projects`` may not import, so it
is injected here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List

from apis.app_api.documents.services.cleanup_service import cleanup_assistant_documents
from apis.app_api.documents.services.document_service import (
    batch_soft_delete_documents,
    list_assistant_documents,
)
from apis.shared.assistants.service import (
    _get_assistant_cloud_without_ownership_check,
    delete_project_harness,
    is_project_harness,
)
from apis.shared.projects.harness import AssistantsHarnessGateway
from apis.shared.sync_policies.service import delete_sync_policies_for_assistant

logger = logging.getLogger(__name__)

_PAGE = 1000


class AppApiHarnessGateway(AssistantsHarnessGateway):
    async def delete(self, agent_id: str) -> None:
        table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
        if not table:
            raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

        harness = await _get_assistant_cloud_without_ownership_check(agent_id, table)
        if harness is None:
            return  # already gone: a retried purge
        if not is_project_harness(harness):
            raise ValueError(f"Agent {agent_id} is not a project harness")

        # Same order as DELETE /assistants: documents and schedules first, record last.
        docs: List = []
        token = None
        while True:
            page, token = await list_assistant_documents(
                assistant_id=agent_id, owner_id=harness.owner_id, limit=_PAGE, next_token=token
            )
            docs.extend(page)
            if not token:
                break
        if docs:
            await batch_soft_delete_documents(
                assistant_id=agent_id, document_ids=[d.document_id for d in docs]
            )
        await delete_sync_policies_for_assistant(agent_id)
        await delete_project_harness(agent_id)

        if docs:
            asyncio.ensure_future(cleanup_assistant_documents(agent_id, docs))
        logger.info("Deleted project harness %s (%d documents queued for cleanup)", agent_id, len(docs))
