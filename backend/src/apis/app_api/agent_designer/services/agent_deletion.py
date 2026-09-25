"""Deleting an agent: every step, in one place, for both delete routes.

``DELETE /agents/{id}`` (what the SPA's Agents page calls) and ``DELETE /assistants/{id}``
(the legacy surface) used to be two copies of this sequence, and they drifted: the
``/agents`` alias only ever deleted the record. Deleting an agent from the UI therefore
left its documents ``complete`` with their S3 objects, its sync policies, and its managed
knowledge base ``ACTIVE`` and billing, with nothing ever cleaning any of it up. Found on
dev on 2026-09-25, after #1293 fixed only the ``/assistants`` copy.

The order is load-bearing:

0. **Refuse first.** ``assert_deletable`` rejects a listed agent or a project's harness
   (``AssistantListedError``, a 409) before anything is touched, and hands back the
   caller's own agent or ``None``. ``None`` is the route's 404, and nothing may be
   cleaned up on the way to it: not someone else's sync policies, not their knowledge
   base.
1. **Queue the managed knowledge base for teardown** before anything destructive, so a
   failure here leaves a delete that can simply be retried. The kb-migration worker does
   the deleting; app-api holds no ``bedrock:DeleteKnowledgeBase`` (see
   ``kb_migration/teardown.py``).
2. **Soft-delete every document**, all pages, not only the first.
3. **Delete sync policies**, so no schedule outlives the agent (the dispatcher's
   liveness check is the backstop, not the mechanism).
4. **Delete the record** (with its versions and reports).
5. **Clean up document vectors and objects in the background.**
6. **Delete the icon objects** (``assistants/{id}/icons/``), best effort and after the
   record, so a failure strands an object rather than failing the delete or leaving a
   live agent without its icon.

A project's harness never comes through here; its project purges it
(``projects/harness_gateway.py``), which runs the same steps.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from apis.app_api.documents.services.document_service import list_assistant_documents
from apis.shared.assistants.icons import delete_agent_icons
from apis.shared.assistants.service import assert_deletable, delete_assistant

logger = logging.getLogger(__name__)

_PAGE = 1000

# Held until each finishes: the event loop keeps only weak references to tasks.
_background: set = set()


async def delete_owned_agent(agent_id: str, owner_id: str) -> bool:
    """Delete the caller's agent and everything it owns. False when it isn't theirs (404).

    Raises ``AssistantListedError`` (including ``ProjectHarnessError``) when the agent
    may not be deleted; nothing has been touched in that case.
    """
    owned = await assert_deletable(agent_id, owner_id)
    if owned is None:
        return False

    from apis.app_api.kb_migration.teardown import queue_teardown

    await queue_teardown(agent_id)

    docs: List = []
    token = None
    while True:
        page, token = await list_assistant_documents(
            assistant_id=agent_id, owner_id=owner_id, limit=_PAGE, next_token=token
        )
        docs.extend(page)
        if not token:
            break
    if docs:
        from apis.app_api.documents.services.document_service import batch_soft_delete_documents

        await batch_soft_delete_documents(
            assistant_id=agent_id, document_ids=[doc.document_id for doc in docs]
        )

    from apis.shared.sync_policies.service import delete_sync_policies_for_assistant

    await delete_sync_policies_for_assistant(agent_id)

    if not await delete_assistant(assistant_id=agent_id, owner_id=owner_id):
        return False

    if docs:
        from apis.app_api.documents.services.cleanup_service import cleanup_assistant_documents

        task = asyncio.ensure_future(cleanup_assistant_documents(agent_id, docs))
        _background.add(task)
        task.add_done_callback(_background.discard)
    icons = await delete_agent_icons(agent_id)
    logger.info(
        "Deleted agent %s (%d documents queued for cleanup, %d icon objects deleted)",
        agent_id, len(docs), icons,
    )
    return True
