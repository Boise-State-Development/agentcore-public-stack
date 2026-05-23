"""User-facing web-source endpoints.

`POST /assistants/{id}/web-sources/crawl` validates the URL, pre-creates the
root `Document` so the SPA has a row to render and poll, persists a
`CrawlJob` row, and fires the BFS crawler as a background task. The endpoint
returns 202 with the root document and the job — exactly mirroring the
shape the SPA already handles for connector imports.

The two GETs (`/crawls`, `/crawls/{id}`) drive the editor's "still
crawling" watcher: the SPA polls active crawls every few seconds and
refreshes its document list, then stops once nothing is `running`.
"""

import asyncio
import logging
from typing import Set

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.app_api.documents.models import (
    DocumentProvenance,
    DocumentResponse,
)
from apis.app_api.documents.services.document_service import (
    _generate_document_id,
    create_document,
)
from apis.app_api.documents.services.storage_service import (
    _get_s3_key,
    _sanitize_filename,
)
from apis.app_api.web_sources.crawl_repository import (
    create_crawl_job,
    get_crawl_job,
    list_active_crawls,
)
from apis.app_api.web_sources.crawler import run_crawl
from apis.app_api.web_sources.models import (
    ActiveCrawlsResponse,
    CrawlJob,
    CrawlSettings,
    StartCrawlRequest,
    StartCrawlResponse,
)
from apis.app_api.web_sources.url_utils import (
    InvalidUrlError,
    assert_url_is_public,
    url_extension_hint,
)
from apis.shared.assistants.service import get_assistant
from apis.shared.auth import User, get_current_user_from_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/assistants/{assistant_id}/web-sources", tags=["web-sources"]
)

# Strong refs to in-flight crawl tasks. Python's event loop tracks tasks
# with weak references, so a bare `asyncio.ensure_future(run_crawl(...))`
# can be garbage-collected mid-execution — leaving the root document
# stuck in 'uploading' forever. Holding the Task in a module-level set
# (and discarding on completion) is the documented workaround.
_BACKGROUND_CRAWLS: Set[asyncio.Task] = set()


@router.post(
    "/crawl",
    response_model=StartCrawlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_crawl(
    assistant_id: str,
    request: StartCrawlRequest,
    current_user: User = Depends(get_current_user_from_session),
) -> StartCrawlResponse:
    """Kick off a web crawl rooted at `request.url` for an assistant.

    Single-page imports (the default toggle in the SPA) are just a crawl
    with `max_depth=0` — the BFS visits only the root and terminates. The
    same async pipeline is used either way so there is no separate code
    path to keep in sync.
    """
    assistant = await get_assistant(assistant_id, current_user.user_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant not found: {assistant_id}",
        )

    try:
        normalized = assert_url_is_public(request.url)
    except InvalidUrlError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )

    settings = request.settings or CrawlSettings()

    document_id = _generate_document_id()
    provisional_filename = f"{_sanitize_filename(url_extension_hint(normalized))}.html"
    s3_key = _get_s3_key(assistant_id, document_id, provisional_filename)
    root_document = await create_document(
        assistant_id=assistant_id,
        filename=provisional_filename,
        content_type="text/html",
        size_bytes=0,
        s3_key=s3_key,
        document_id=document_id,
        provenance=DocumentProvenance(
            source_connector_id="web",
            source_adapter_key="http",
            source_file_id=normalized,
            imported_by_user_id=current_user.user_id,
        ),
    )

    job = await create_crawl_job(
        assistant_id=assistant_id,
        root_url=normalized,
        settings=settings,
        started_by_user_id=current_user.user_id,
    )

    task = asyncio.create_task(
        run_crawl(
            assistant_id=assistant_id,
            crawl_id=job.crawl_id,
            user_id=current_user.user_id,
            root_url=normalized,
            settings=settings,
            root_document_id=document_id,
        )
    )
    _BACKGROUND_CRAWLS.add(task)
    task.add_done_callback(_BACKGROUND_CRAWLS.discard)
    logger.info(
        "Kicked off crawl %s for assistant %s (root_document=%s url=%s)",
        job.crawl_id,
        assistant_id,
        document_id,
        normalized,
    )

    return StartCrawlResponse(
        crawl=job,
        documents=[
            DocumentResponse.model_validate(root_document.model_dump(by_alias=True))
        ],
    )


@router.get("/crawls", response_model=ActiveCrawlsResponse)
async def list_crawls(
    assistant_id: str,
    active: bool = Query(False, description="Return only `running` jobs"),
    current_user: User = Depends(get_current_user_from_session),
) -> ActiveCrawlsResponse:
    """List crawl jobs for an assistant.

    `?active=true` is the only filter currently honored — drives the SPA's
    "should I keep polling for new docs" decision.
    """
    assistant = await get_assistant(assistant_id, current_user.user_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant not found: {assistant_id}",
        )
    if active:
        crawls = await list_active_crawls(assistant_id)
    else:
        # The full-history view is reserved for a future "crawl history"
        # panel; for now we return active only when asked and an empty list
        # otherwise to keep the contract small.
        crawls = await list_active_crawls(assistant_id)
    return ActiveCrawlsResponse(crawls=crawls)


@router.get("/crawls/{crawl_id}", response_model=CrawlJob)
async def get_crawl(
    assistant_id: str,
    crawl_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> CrawlJob:
    """Return a single crawl's current status + counters."""
    assistant = await get_assistant(assistant_id, current_user.user_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant not found: {assistant_id}",
        )
    job = await get_crawl_job(assistant_id, crawl_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Crawl not found: {crawl_id}",
        )
    return job
