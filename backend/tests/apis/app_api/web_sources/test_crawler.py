"""Crawler tests with httpx routed through an in-process MockTransport.

We pin every external dependency the crawler reaches for:
- httpx fetches → MockTransport returning canned HTML
- create_document / update_document_* / update_document_status →
  in-memory recorder (avoids DynamoDB)
- S3 PUT → no-op stub
- crawl_repository.* counters / finalize → in-memory recorder
- asyncio.sleep → monkeypatched to a no-op so jitter delays don't slow
  the suite

This lets us assert: BFS visits the right URLs, robots.txt is honored,
max_pages / max_depth / same_domain_only gates work, per-page failures
mark the doc failed without aborting the crawl, and the job is always
finalized.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Tuple

import httpx
import pytest

from apis.app_api.web_sources import crawl_repository, crawler
from apis.app_api.web_sources.crawler import run_crawl
from apis.app_api.web_sources.models import CrawlSettings


# ── Recorders ──────────────────────────────────────────────────────────────


class _Recorder:
    """Captures every side-effect the crawler attempts so tests can assert."""

    def __init__(self) -> None:
        self.created_docs: List[Tuple[str, str]] = []  # (document_id, source_url)
        self.status_updates: List[Tuple[str, str]] = []  # (document_id, status)
        self.metadata_updates: List[str] = []  # document_id list
        self.s3_puts: List[Tuple[str, bytes]] = []  # (s3_key, body)
        self.discovered_delta = 0
        self.fetched_delta = 0
        self.failed_delta = 0
        self.finalized_status: str | None = None
        self.finalized_error: str | None = None
        self._doc_counter = 0
        self.preassigned_root_id = "DOC-root00000001"

    def next_doc_id(self) -> str:
        self._doc_counter += 1
        return f"DOC-doc{self._doc_counter:09d}"


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    async def fake_create_document(
        *,
        assistant_id: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        s3_key: str,
        document_id: str | None = None,
        provenance=None,
    ):
        document_id = document_id or rec.next_doc_id()
        rec.created_docs.append(
            (document_id, provenance.source_file_id if provenance else "")
        )

        class _Stub:
            pass

        stub = _Stub()
        stub.document_id = document_id
        return stub

    async def fake_update_status(
        *,
        assistant_id: str,
        document_id: str,
        status: str,
        error_message: str | None = None,
        error_details: str | None = None,
        vector_store_id: str | None = None,
        chunk_count: int | None = None,
        table_name: str | None = None,
    ):
        rec.status_updates.append((document_id, status))
        return None

    async def fake_update_metadata(
        assistant_id: str,
        document_id: str,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        s3_key: str,
        source_etag: str | None = None,
    ):
        rec.metadata_updates.append(document_id)
        return None

    counter = {"n": 0}

    def fake_generate_document_id() -> str:
        counter["n"] += 1
        return f"DOC-gen{counter['n']:09d}"

    async def fake_put_markdown(
        *, assistant_id: str, document_id: str, markdown: str, filename: str
    ) -> str:
        key = f"assistants/{assistant_id}/documents/{document_id}/{filename}"
        rec.s3_puts.append((key, markdown.encode("utf-8")))
        return key

    async def fake_increment(
        *,
        assistant_id: str,
        crawl_id: str,
        discovered_delta: int = 0,
        fetched_delta: int = 0,
        failed_delta: int = 0,
    ):
        rec.discovered_delta += discovered_delta
        rec.fetched_delta += fetched_delta
        rec.failed_delta += failed_delta

    async def fake_finalize(
        *, assistant_id: str, crawl_id: str, status: str, error: str | None = None
    ):
        rec.finalized_status = status
        rec.finalized_error = error

    # Crawler module reaches via the import names it owns. Patch each one
    # on the crawler module so the BFS reroutes uniformly.
    monkeypatch.setattr(crawler, "create_document", fake_create_document)
    monkeypatch.setattr(crawler, "update_document_status", fake_update_status)
    monkeypatch.setattr(crawler, "update_document_import_metadata", fake_update_metadata)
    monkeypatch.setattr(crawler, "_put_markdown", fake_put_markdown)
    monkeypatch.setattr(crawler, "increment_counters", fake_increment)
    monkeypatch.setattr(crawler, "finalize_crawl", fake_finalize)
    # _create_pending_document calls _generate_document_id, which lives in
    # the documents service module; patch the symbol the crawler closes over.
    monkeypatch.setattr(
        "apis.app_api.documents.services.document_service._generate_document_id",
        fake_generate_document_id,
    )

    # Skip all delays — tests must complete in milliseconds, not seconds.
    real_sleep = asyncio.sleep

    async def no_sleep(seconds: float = 0):
        # Keep zero-second yields for cooperative scheduling.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    return rec


# ── Transport helpers ──────────────────────────────────────────────────────


def _build_handler(pages: Dict[str, str], status_codes: Dict[str, int] | None = None):
    """Return a `httpx.MockTransport` handler that serves canned pages.

    Missing URLs return 404. robots.txt defaults to "all allowed" unless
    a `pages` entry overrides it.
    """
    status_codes = status_codes or {}

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt") and url not in pages:
            return httpx.Response(404, text="")
        if url in pages:
            code = status_codes.get(url, 200)
            return httpx.Response(
                code,
                text=pages[url],
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, text="not found")

    return _handler


def _client_factory(handler):
    def _factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return _factory


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_page_crawl_writes_one_doc(recorder: _Recorder):
    pages = {
        "https://example.com/": "<html><head><title>Home</title></head><body><p>hi</p></body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=0, max_pages=1),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.finalized_status == "complete"
    # Root document was created by the route (not by the crawler), so no
    # new doc records here. One S3 PUT for the root.
    assert recorder.created_docs == []
    assert len(recorder.s3_puts) == 1
    assert recorder.fetched_delta == 1
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_bfs_follows_links_within_depth(recorder: _Recorder):
    pages = {
        "https://example.com/": (
            "<html><head><title>Home</title></head><body>"
            '<a href="/a">A</a><a href="/b">B</a>'
            "</body></html>"
        ),
        "https://example.com/a": "<html><head><title>A</title></head><body>a</body></html>",
        "https://example.com/b": "<html><head><title>B</title></head><body>b</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=10, concurrency=2),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.finalized_status == "complete"
    assert recorder.fetched_delta == 3  # root + 2 children
    assert recorder.failed_delta == 0
    fetched_urls = {url for _, url in recorder.created_docs}
    assert fetched_urls == {"https://example.com/a", "https://example.com/b"}


@pytest.mark.asyncio
async def test_max_depth_zero_skips_links(recorder: _Recorder):
    pages = {
        "https://example.com/": (
            "<html><body><a href='/a'>A</a></body></html>"
        ),
        "https://example.com/a": "<html><body>a</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=0, max_pages=10),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.fetched_delta == 1
    assert recorder.created_docs == []  # no children created


@pytest.mark.asyncio
async def test_max_pages_caps_visits(recorder: _Recorder):
    # Root links to 5 pages, max_pages=3 → root + 2 children.
    pages = {
        "https://example.com/": (
            "<html><body>"
            + "".join(f'<a href="/{i}">{i}</a>' for i in range(5))
            + "</body></html>"
        ),
        **{
            f"https://example.com/{i}": f"<html><body>{i}</body></html>"
            for i in range(5)
        },
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=3),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.fetched_delta == 3


@pytest.mark.asyncio
async def test_same_domain_filter_drops_external_links(recorder: _Recorder):
    pages = {
        "https://example.com/": (
            "<html><body>"
            '<a href="https://example.com/keep">k</a>'
            '<a href="https://other.com/drop">d</a>'
            "</body></html>"
        ),
        "https://example.com/keep": "<html><body>keep</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(
            max_depth=1, max_pages=10, same_domain_only=True
        ),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.fetched_delta == 2
    fetched_urls = {url for _, url in recorder.created_docs}
    assert fetched_urls == {"https://example.com/keep"}


@pytest.mark.asyncio
async def test_per_page_404_marks_doc_failed_but_continues(recorder: _Recorder):
    pages = {
        "https://example.com/": (
            "<html><body>"
            '<a href="/missing">m</a><a href="/ok">o</a>'
            "</body></html>"
        ),
        "https://example.com/ok": "<html><body>ok</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=10),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.finalized_status == "complete"
    # Root + ok succeed; /missing fails.
    assert recorder.fetched_delta == 2
    assert recorder.failed_delta == 1
    statuses = {doc_id: st for doc_id, st in recorder.status_updates}
    assert "failed" in statuses.values()


@pytest.mark.asyncio
async def test_robots_disallow_skips_url(recorder: _Recorder):
    pages = {
        "https://example.com/robots.txt": "User-agent: *\nDisallow: /private",
        "https://example.com/": (
            "<html><body>"
            '<a href="/private">p</a><a href="/public">u</a>'
            "</body></html>"
        ),
        "https://example.com/public": "<html><body>public</body></html>",
        "https://example.com/private": "<html><body>private</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=10),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    fetched_urls = {url for _, url in recorder.created_docs}
    assert "https://example.com/private" not in fetched_urls
    # No doc was ever created for /private, so failed_delta stays at 0
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_robots_disallow_root_fails_pre_created_doc(recorder: _Recorder):
    pages = {
        "https://example.com/robots.txt": "User-agent: *\nDisallow: /",
        "https://example.com/": "<html><body>nope</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=0, max_pages=1),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    # Root doc was disallowed → status update should mark it failed.
    statuses = {doc_id: st for doc_id, st in recorder.status_updates}
    assert statuses.get(recorder.preassigned_root_id) == "failed"
    assert recorder.failed_delta == 1


@pytest.mark.asyncio
async def test_finalize_runs_on_exception(monkeypatch: pytest.MonkeyPatch):
    """Even if the inner loop crashes, the CrawlJob must not stay 'running'."""
    finalized: List[str] = []

    async def fake_finalize(*, assistant_id, crawl_id, status, error=None):
        finalized.append(status)

    async def fake_increment(**kwargs):
        return None

    real_sleep = asyncio.sleep

    async def no_sleep(*_a, **_kw):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(crawler, "finalize_crawl", fake_finalize)
    monkeypatch.setattr(crawler, "increment_counters", fake_increment)

    async def fake_create(**kwargs):  # never called — we explode first
        raise RuntimeError("boom from create_document")

    monkeypatch.setattr(crawler, "create_document", fake_create)

    # Force the inner loop to raise: pass a client factory that raises
    # on instantiation.
    def explode_factory():
        raise RuntimeError("transport explode")

    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(),
        root_document_id="DOC-root",
        http_client_factory=explode_factory,
    )
    assert finalized == ["failed"]


@pytest.mark.asyncio
async def test_visited_dedupes_repeated_links(recorder: _Recorder):
    pages = {
        "https://example.com/": (
            "<html><body>"
            '<a href="/a">a1</a><a href="/a">a2</a><a href="/a#frag">a3</a>'
            "</body></html>"
        ),
        "https://example.com/a": "<html><body>a</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=10),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    # Only one extra doc despite three duplicate <a>'s.
    assert len(recorder.created_docs) == 1
