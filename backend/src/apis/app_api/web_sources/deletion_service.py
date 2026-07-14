"""Removal of a whole web source — the crawl record plus every page it ingested.

A "web source" is not a single stored entity: it is a `CrawlJob` row, the
fan-out of `Document` rows the crawler wrote beneath it, and (optionally) a
`web_crawl` sync policy keyed on the crawl id. Removing one therefore has to
walk that fan-out itself.

The reverse relationship already exists: deleting the *last* page of a crawl
lets `documents.services.cleanup_service._cascade_delete_orphaned_crawl_jobs`
drop the now-orphaned `CrawlJob`. This module drives the same graph from the
other end — delete the source, and the pages go with it.

Pages are matched to their crawl by URL prefix (`source_file_id` starts with
the crawl's `root_url`), the same rule the orphan cascade uses and sound for
the same reason: the crawler only enqueues URLs that already passed its
same-domain / same-root filter.
"""

import asyncio
import logging
from typing import List, Set

from apis.app_api.documents.models import Document
from apis.app_api.documents.services.cleanup_service import cleanup_assistant_documents
from apis.app_api.documents.services.document_service import (
    list_assistant_documents,
    soft_delete_document,
)
from apis.app_api.web_sources.crawl_repository import hard_delete_crawl_job
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.sync_policies.service import delete_sync_policies_for_source

logger = logging.getLogger(__name__)

# Strong refs to the fire-and-forget cleanup task. The event loop only holds
# weak references to tasks, so a bare `create_task` can be collected mid-run —
# the same trap the route documents for its background crawls.
_BACKGROUND_CLEANUPS: Set[asyncio.Task] = set()


class WebSourceDeletionError(Exception):
    """The crawl record itself could not be removed."""


async def delete_web_source(
    *,
    assistant_id: str,
    crawl_id: str,
    root_url: str,
    owner_id: str,
) -> int:
    """Remove a web source and everything it owns. Returns the page count removed.

    Order matters:

    1. The sync policy goes first. It keys on the crawl id, so a dispatcher
       sweep landing mid-delete would otherwise re-crawl the very source we
       are removing and resurrect its pages.
    2. Pages are soft-deleted (status `deleting` + TTL) so they leave the KB
       immediately, with the slow half — vectors and S3 objects — handed to a
       background task, exactly as the single-document delete path does.
    3. The crawl row is hard-deleted last. If a soft-delete fails partway, the
       surviving row still lists the source and the user can retry; deleting
       the row first would strand its pages with nothing to remove them from.

    Raises `WebSourceDeletionError` if the crawl row survives — leaving it
    behind would show the caller a source that is now empty but still listed.
    """
    pages = await _list_crawl_pages(assistant_id, owner_id, root_url)

    policies_removed = await delete_sync_policies_for_source(assistant_id, crawl_id)

    deleted: List[Document] = []
    for page in pages:
        document = await soft_delete_document(assistant_id, page.document_id, owner_id)
        if document:
            deleted.append(document)

    if not await hard_delete_crawl_job(assistant_id, crawl_id):
        raise WebSourceDeletionError(
            "Removed the pages but could not remove the web source record. "
            "Try again in a moment."
        )

    # Vectors + S3, off the request path. `cleanup_assistant_documents` gathers
    # per-document cleanup and never raises; it also hard-deletes each DynamoDB
    # row on success. It is deliberately *not* given the docs' `web` connector
    # id — that would fire the orphaned-CrawlJob cascade once per page (a full
    # document scan each), and we just deleted the crawl row ourselves.
    if deleted:
        task = asyncio.create_task(cleanup_assistant_documents(assistant_id, deleted))
        _BACKGROUND_CLEANUPS.add(task)
        task.add_done_callback(_BACKGROUND_CLEANUPS.discard)

    logger.info(
        "Removed web source %s from assistant %s (root=%s, pages=%d, sync_policies=%d)",
        scrub_log(crawl_id),
        scrub_log(assistant_id),
        scrub_log(root_url),
        len(deleted),
        policies_removed,
    )
    return len(deleted)


async def _list_crawl_pages(
    assistant_id: str, owner_id: str, root_url: str
) -> List[Document]:
    """Every live document this crawl produced, across all pages of results.

    Rows already in `deleting` are skipped — their cleanup is in flight and
    re-soft-deleting them would just reset the TTL.
    """
    pages: List[Document] = []
    next_token = None
    while True:
        batch, next_token = await list_assistant_documents(
            assistant_id, owner_id, next_token=next_token
        )
        for document in batch:
            if document.source_connector_id != "web":
                continue
            if not document.source_file_id:
                continue
            if not document.source_file_id.startswith(root_url):
                continue
            if document.status == "deleting":
                continue
            pages.append(document)
        if not next_token:
            break
    return pages
