"""Pydantic models for the web-crawl ingestion API.

`CrawlSettings` is the single source of truth for the legal ranges of every
knob — the same ranges drive the SPA's sliders, so changing a bound here is
the only edit needed. The route validates incoming requests against these
bounds before any I/O.

`CrawlJob` mirrors the DynamoDB row 1:1 — see `crawl_repository.py`. It's
returned to the SPA so the editor can show progress and stop its watcher
loop when the job leaves `running`.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apis.app_api.documents.models import DocumentResponse

CrawlJobStatus = Literal["running", "complete", "failed"]


class CrawlSettings(BaseModel):
    """Tunable bounds for a single crawl job.

    Defaults are deliberately polite: depth 2 / 25 pages / 2 concurrent
    fetches / 1–3 s jitter. The hard caps protect both us and the target
    site — wider knobs would need a per-host rate-limit story we don't have
    yet.
    """

    model_config = ConfigDict(populate_by_name=True)

    max_depth: int = Field(2, alias="maxDepth", ge=0, le=3)
    max_pages: int = Field(25, alias="maxPages", ge=1, le=100)
    concurrency: int = Field(2, ge=1, le=5)
    min_delay_seconds: float = Field(1.0, alias="minDelay", ge=0.0, le=5.0)
    max_delay_seconds: float = Field(3.0, alias="maxDelay", ge=0.0, le=10.0)
    same_domain_only: bool = Field(True, alias="sameDomainOnly")

    @model_validator(mode="after")
    def _delay_ordering(self) -> "CrawlSettings":
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("maxDelay must be >= minDelay")
        return self


class StartCrawlRequest(BaseModel):
    """Request body for `POST /assistants/{id}/web-sources/crawl`.

    When `settings` is None the server applies `CrawlSettings()` defaults —
    the SPA does the same so the single-page case can send `{ url }` only.
    """

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(..., min_length=1, max_length=2048)
    settings: Optional[CrawlSettings] = None


class CrawlJob(BaseModel):
    """A web-crawl job persisted as one DynamoDB row alongside the assistant.

    Stored at `PK=AST#{assistant_id}, SK=CRAWL#{crawl_id}` so the editor's
    list-active-crawls query (`SK begins_with CRAWL#`) is cheap.
    """

    model_config = ConfigDict(populate_by_name=True)

    crawl_id: str = Field(..., alias="crawlId")
    assistant_id: str = Field(..., alias="assistantId")
    root_url: str = Field(..., alias="rootUrl")
    status: CrawlJobStatus
    settings: CrawlSettings
    discovered_count: int = Field(0, alias="discoveredCount", ge=0)
    fetched_count: int = Field(0, alias="fetchedCount", ge=0)
    failed_count: int = Field(0, alias="failedCount", ge=0)
    started_at: str = Field(..., alias="startedAt")
    completed_at: Optional[str] = Field(None, alias="completedAt")
    started_by_user_id: str = Field(..., alias="startedByUserId")
    error: Optional[str] = None


class StartCrawlResponse(BaseModel):
    """Returned synchronously from the crawl-start endpoint.

    Includes the freshly-created root `Document` so the SPA can render and
    poll it like a connector import; `crawl` lets the editor's watcher loop
    know a crawl is in flight.
    """

    model_config = ConfigDict(populate_by_name=True)

    crawl: CrawlJob
    documents: List[DocumentResponse]


class ActiveCrawlsResponse(BaseModel):
    """List of crawls currently `running` for an assistant.

    The editor polls this every few seconds while it has any web-imported
    document still in a processing state, then stops once the list is empty.
    """

    model_config = ConfigDict(populate_by_name=True)

    crawls: List[CrawlJob]
