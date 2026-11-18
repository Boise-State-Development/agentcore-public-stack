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
        self.created_statuses: List[str] = []  # the status each new page starts at
        self.kb_record: dict | None = None  # the knowledge-base record a new page reads
        self.s3_content_types: List[str] = []  # the ContentType of each S3 PUT, in order
        self.metadata_calls: List[dict] = []  # every update_document_import_metadata kwargs
        self.status_messages: Dict[str, str | None] = {}  # document_id -> last error_message

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
        status: str = "uploading",
    ):
        document_id = document_id or rec.next_doc_id()
        rec.created_docs.append(
            (document_id, provenance.source_file_id if provenance else "")
        )
        rec.created_statuses.append(status)

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
        rec.status_messages[document_id] = error_message
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
        rec.metadata_calls.append(
            {"document_id": document_id, "filename": filename, "content_type": content_type, "size_bytes": size_bytes}
        )
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
        rec.s3_content_types.append("text/markdown")
        return key

    async def fake_put_pdf(*, assistant_id: str, document_id: str, body: bytes, filename: str) -> str:
        key = f"assistants/{assistant_id}/documents/{document_id}/{filename}"
        rec.s3_puts.append((key, body))
        rec.s3_content_types.append("application/pdf")
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
        *, assistant_id: str, crawl_id: str, status: str, error: str | None = None,
        set_ttl: bool = True,
    ):
        rec.finalized_status = status
        rec.finalized_error = error
        rec.finalized_set_ttl = set_ttl

    # Crawler module reaches via the import names it owns. Patch each one
    # on the crawler module so the BFS reroutes uniformly.
    monkeypatch.setattr(crawler, "create_document", fake_create_document)
    monkeypatch.setattr(crawler, "update_document_status", fake_update_status)
    monkeypatch.setattr(crawler, "update_document_import_metadata", fake_update_metadata)
    monkeypatch.setattr(crawler, "_put_markdown", fake_put_markdown)
    monkeypatch.setattr(crawler, "_put_pdf", fake_put_pdf)
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
    # Each new page reads the knowledge-base record to pick its starting status.
    monkeypatch.setattr(
        "apis.shared.kb_backend.records.get_kb_record",
        lambda assistant_id, app_kb_id: rec.kb_record,
    )

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


@pytest.mark.parametrize(
    "kb_record,expected",
    [
        (None, "uploading"),  # legacy: no record
        ({"retrievalEngine": "managed", "awsKbId": "kb", "awsDataSourceId": "ds"}, "uploading"),  # built
        ({"retrievalEngine": "managed"}, "provisioning"),  # being born managed
    ],
)
@pytest.mark.asyncio
async def test_pages_wait_on_a_knowledge_base_still_being_provisioned(recorder: _Recorder, kb_record, expected):
    # Otherwise the managed consumer defers them and the provisioning job, which
    # ingests only `provisioning` rows, never picks them up.
    recorder.kb_record = kb_record
    pages = {
        "https://example.com/": '<html><body><a href="/a">A</a></body></html>',
        "https://example.com/a": "<html><head><title>A</title></head><body>a</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=5),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.created_statuses == [expected]


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
async def test_crawl_stays_inside_the_start_urls_section(recorder: _Recorder):
    # A crawl started at /research/ must not follow the site nav to the home
    # page or a sibling section and spend its page budget there.
    nav = (
        '<a href="https://www.example.com/">Home</a>'
        '<a href="/admissions/">Admissions</a>'
        '<a href="/research-news/">News</a>'
    )
    pages = {
        "https://example.com/research/": (
            f"<html><body>{nav}"
            '<a href="/research/labs">Labs</a>'
            '<a href="https://www.example.com/research/grants">Grants</a>'
            "</body></html>"
        ),
        "https://example.com/research/labs": f"<html><body>{nav}labs</body></html>",
        "https://www.example.com/research/grants": f"<html><body>{nav}grants</body></html>",
        "https://www.example.com/": "<html><body>home</body></html>",
        "https://example.com/admissions/": "<html><body>admissions</body></html>",
        "https://example.com/research-news/": "<html><body>news</body></html>",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/research/",
        settings=CrawlSettings(max_depth=2, max_pages=25),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_build_handler(pages)),
    )
    assert recorder.finalized_status == "complete"
    fetched_urls = {url for _, url in recorder.created_docs}
    assert fetched_urls == {
        "https://example.com/research/labs",
        "https://www.example.com/research/grants",
    }


@pytest.mark.asyncio
async def test_a_home_page_start_crawls_the_whole_host(recorder: _Recorder):
    pages = {
        "https://example.com/": (
            "<html><body>"
            '<a href="/admissions/">A</a><a href="/research/">R</a>'
            '<a href="https://docs.example.com/">Docs</a>'
            "</body></html>"
        ),
        "https://example.com/admissions/": "<html><body>a</body></html>",
        "https://example.com/research/": "<html><body>r</body></html>",
        "https://docs.example.com/": "<html><body>d</body></html>",
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
    assert fetched_urls == {
        "https://example.com/admissions/",
        "https://example.com/research/",
    }


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

    async def fake_finalize(*, assistant_id, crawl_id, status, error=None, set_ttl=True):
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


# ── KB-sync refresh mode ────────────────────────────────────────────────────


def _build_refresh_handler(pages: Dict[str, str], etags: Dict[str, str] | None = None):
    """Like _build_handler, but ETag-aware: a request whose If-None-Match
    matches the page's etag gets a 304 with no body."""
    etags = etags or {}

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt") and url not in pages:
            return httpx.Response(404, text="")
        if url not in pages:
            return httpx.Response(404, text="not found")
        etag = etags.get(url)
        if etag and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        headers = {"content-type": "text/html; charset=utf-8"}
        if etag:
            headers["etag"] = etag
        return httpx.Response(200, text=pages[url], headers=headers)

    return _handler


class _ResultLog:
    """Captures RefreshState.on_result emissions with staging order info."""

    def __init__(self, rec: _Recorder):
        self.rec = rec
        self.events: List[Tuple[str, str, int]] = []  # (outcome, url, s3_puts_at_emit)

    async def __call__(self, url, document_id, outcome, etag, content_hash):
        self.events.append((outcome, url, len(self.rec.s3_puts)))


def _refresh_state(rec: _Recorder, docs: Dict[str, crawler.RefreshDoc]):
    log = _ResultLog(rec)
    state = crawler.RefreshState(docs=docs, on_result=log)
    return state, log


ROOT = "https://example.com/"


async def _run_refresh(rec: _Recorder, pages, state, *, etags=None, settings=None):
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=ROOT,
        settings=settings or CrawlSettings(max_depth=1, max_pages=10),
        root_document_id="DOC-root",
        http_client_factory=_client_factory(_build_refresh_handler(pages, etags)),
        refresh=state,
        finalize_with_ttl=False,
    )


@pytest.mark.asyncio
async def test_refresh_304_skips_stage_and_counts_unchanged(recorder: _Recorder):
    pages = {ROOT: "<html><body>hi</body></html>"}
    etags = {ROOT: '"v1"'}
    state, log = _refresh_state(
        recorder, {ROOT: crawler.RefreshDoc(document_id="DOC-root", source_etag='"v1"')}
    )

    await _run_refresh(recorder, pages, state, etags=etags)

    assert state.unchanged == 1 and state.changed == 0 and state.created == 0
    assert recorder.s3_puts == []
    assert recorder.created_docs == []
    assert ROOT in state.seen_urls
    assert recorder.finalized_set_ttl is False


@pytest.mark.asyncio
async def test_refresh_identical_hash_skips_stage(recorder: _Recorder):
    html = "<html><head><title>T</title></head><body><p>same content</p></body></html>"
    markdown, _ = crawler._extract_markdown(html, ROOT)
    stored_hash = crawler._content_sha256(markdown)
    state, log = _refresh_state(
        recorder,
        {ROOT: crawler.RefreshDoc(document_id="DOC-root", source_etag='"old"', content_hash=stored_hash)},
    )

    # Server serves a NEW etag (so the 304 gate misses) but identical content.
    await _run_refresh(recorder, {ROOT: html}, state, etags={ROOT: '"new"'})

    assert state.unchanged == 1
    assert recorder.s3_puts == []
    # etag advanced via on_result so next run's conditional GET can 304
    assert log.events == [("unchanged", ROOT, 0)]


@pytest.mark.asyncio
async def test_refresh_changed_page_restages_and_emits_before_put(recorder: _Recorder):
    state, log = _refresh_state(
        recorder,
        {ROOT: crawler.RefreshDoc(document_id="DOC-root", source_etag='"old"', content_hash="different", chunk_count=5)},
    )

    await _run_refresh(
        recorder, {ROOT: "<html><body><p>brand new words</p></body></html>"}, state, etags={ROOT: '"new"'}
    )

    assert state.changed == 1
    assert len(recorder.s3_puts) == 1
    # "changed" emitted BEFORE staging so the worker can stash the previous
    # chunk count before the S3 event fires ingestion
    assert log.events[0][0] == "changed" and log.events[0][2] == 0


@pytest.mark.asyncio
async def test_refresh_reuses_existing_docs_and_creates_new_ones(recorder: _Recorder):
    pages = {
        ROOT: '<html><body><a href="/a">A</a><a href="/new">N</a></body></html>',
        "https://example.com/a": "<html><body>a-page</body></html>",
        "https://example.com/new": "<html><body>new-page</body></html>",
    }
    state, log = _refresh_state(
        recorder,
        {
            ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash="stale"),
            "https://example.com/a": crawler.RefreshDoc(document_id="DOC-a", content_hash="stale"),
        },
    )

    await _run_refresh(recorder, pages, state)

    # Known URLs reuse their records; only /new creates a document.
    assert [u for _id, u in recorder.created_docs] == ["https://example.com/new"]
    assert state.created == 1 and state.changed == 2
    assert state.seen_urls == {ROOT, "https://example.com/a", "https://example.com/new"}


@pytest.mark.asyncio
async def test_refresh_fetch_error_leaves_existing_doc_untouched(recorder: _Recorder):
    pages = {ROOT: "<html><body>ok</body></html>", "https://example.com/broken": "irrelevant"}
    state, _ = _refresh_state(
        recorder,
        {
            ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash="stale"),
            "https://example.com/broken": crawler.RefreshDoc(document_id="DOC-broken", content_hash="x"),
        },
    )
    pages_with_link = dict(pages)
    pages_with_link[ROOT] = '<html><body><a href="/broken">b</a></body></html>'

    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=10),
        root_document_id="DOC-root",
        http_client_factory=_client_factory(
            _build_handler(pages_with_link, status_codes={"https://example.com/broken": 500})
        ),
        refresh=state,
        finalize_with_ttl=False,
    )

    # The existing doc must NOT be flipped to failed on a transient error…
    assert ("DOC-broken", "failed") not in recorder.status_updates
    # …and it still counts as seen (an erroring page is not a missing page).
    assert "https://example.com/broken" in state.seen_urls


@pytest.mark.asyncio
async def test_refresh_robots_disallow_is_not_seen(recorder: _Recorder):
    pages = {
        "https://example.com/robots.txt": "User-agent: *\nDisallow: /",
        ROOT: "<html><body>hi</body></html>",
    }
    state, _ = _refresh_state(
        recorder, {ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash="h")}
    )

    await _run_refresh(recorder, pages, state)

    # Robots said stop indexing: the URL is deliberately NOT seen, so the
    # worker's miss counter starts ticking toward removal.
    assert state.seen_urls == set()


@pytest.mark.asyncio
async def test_refresh_stage_failure_emits_stage_failed(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
):
    async def failing_put(*, assistant_id, document_id, markdown, filename):
        raise RuntimeError("S3 PutObject failed")

    monkeypatch.setattr(crawler, "_put_markdown", failing_put)
    pages = {
        ROOT: '<html><body><p>changed words</p><a href="/new">n</a></body></html>',
        "https://example.com/new": "<html><body><p>new page</p></body></html>",
    }
    state, log = _refresh_state(
        recorder, {ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash="different")}
    )

    await _run_refresh(recorder, pages, state)

    # Each pre-stage emit is followed by stage_failed, so the worker can roll
    # back the gate values it wrote for bytes that never reached S3.
    outcomes = sorted((outcome, url) for outcome, url, _ in log.events)
    assert outcomes == [
        ("changed", ROOT),
        ("created", "https://example.com/new"),
        ("stage_failed", ROOT),
        ("stage_failed", "https://example.com/new"),
    ]
    assert state.stage_failed == 2
    assert recorder.failed_delta == 2
    assert recorder.metadata_updates == []
    # The existing page keeps serving its last-good version; the new page,
    # which has nothing staged at all, is marked failed.
    new_doc_id = recorder.created_docs[0][0]
    assert (new_doc_id, "failed") in recorder.status_updates
    assert ("DOC-root", "failed") not in recorder.status_updates
    assert recorder.finalized_status == "complete"


# ── Redirects ──────────────────────────────────────────────────────────────


def _redirecting_handler(pages: Dict[str, str], redirects: Dict[str, str], etags: Dict[str, str] | None = None):
    """Serve `pages`, and 301 each `redirects` key to its value.

    The shape of a real site: boisestate.edu 301s to www.boisestate.edu and
    `/x` to `/x/`, while its pages link to more than one form of each.
    """
    etags = etags or {}
    requested: List[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        requested.append(url)
        if url in redirects:
            return httpx.Response(301, headers={"location": redirects[url]})
        if url in pages:
            etag = etags.get(url)
            if etag and request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            headers = {"content-type": "text/html; charset=utf-8"}
            if etag:
                headers["etag"] = etag
            return httpx.Response(200, text=pages[url], headers=headers)
        return httpx.Response(404, text="not found")

    _handler.requested = requested  # type: ignore[attr-defined]
    return _handler


_SECTION_ROOT = "https://example.com/research"
_WWW = "https://www.example.com/research"


@pytest.mark.asyncio
async def test_a_page_reached_through_two_urls_gets_one_document(recorder: _Recorder):
    pages = {
        f"{_WWW}/": (
            "<html><body>"
            '<a href="/research/labs/">Labs</a>'
            '<a href="https://example.com/research/labs/">Labs again</a>'
            '<a href="/research/grants">Grants</a>'
            '<a href="/research/grants/">Grants again</a>'
            f'<a href="{_WWW}/">Home of research</a>'
            "</body></html>"
        ),
        f"{_WWW}/labs/": "<html><body>labs</body></html>",
        f"{_WWW}/grants/": "<html><body>grants</body></html>",
    }
    redirects = {
        _SECTION_ROOT: f"{_WWW}/",
        "https://example.com/research/labs/": f"{_WWW}/labs/",
        f"{_WWW}/grants": f"{_WWW}/grants/",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=25),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_redirecting_handler(pages, redirects)),
    )
    assert recorder.finalized_status == "complete"
    # Recorded under where each page landed; the root keeps the route's doc.
    assert sorted(url for _, url in recorder.created_docs) == [
        f"{_WWW}/grants/",
        f"{_WWW}/labs/",
    ]
    assert len(recorder.s3_puts) == 3
    # The counters describe pages, not URLs: duplicates gave their slot back.
    assert recorder.discovered_delta == recorder.fetched_delta == 3
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_a_redirect_out_of_the_crawl_is_not_ingested(recorder: _Recorder):
    pages = {
        f"{_WWW}/": (
            "<html><body>"
            '<a href="/research/moved">Moved</a>'
            '<a href="/research/metadata">Metadata</a>'
            '<a href="/research/kept">Kept</a>'
            "</body></html>"
        ),
        "https://www.example.com/admissions/": "<html><body>admissions</body></html>",
        "http://169.254.169.254/latest": "<html><body>credentials</body></html>",
        f"{_WWW}/kept": "<html><body>kept</body></html>",
    }
    redirects = {
        _SECTION_ROOT: f"{_WWW}/",
        f"{_WWW}/moved": "https://www.example.com/admissions/",
        f"{_WWW}/metadata": "http://169.254.169.254/latest",
    }
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=25),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_redirecting_handler(pages, redirects)),
    )
    assert [url for _, url in recorder.created_docs] == [f"{_WWW}/kept"]
    assert all(b"credentials" not in body and b"admissions" not in body for _, body in recorder.s3_puts)
    assert recorder.discovered_delta == recorder.fetched_delta == 2


@pytest.mark.asyncio
async def test_refresh_moves_a_redirected_page_to_where_it_landed(recorder: _Recorder):
    # A crawl from before this fix stored the same page twice. The re-crawl
    # sees only the URL it lands on, so the miss counter retires the other.
    bare, www = "https://example.com/research/a", f"{_WWW}/a"
    html = "<html><body><p>a</p></body></html>"
    markdown, _ = crawler._extract_markdown(html, www)
    stored_hash = crawler._content_sha256(markdown)
    pages = {
        f"{_WWW}/": f'<html><body><a href="{bare}">A</a><a href="{www}">A</a></body></html>',
        www: html,
    }
    redirects = {_SECTION_ROOT: f"{_WWW}/", bare: www}
    etags = {www: '"v1"'}
    state, log = _refresh_state(
        recorder,
        {
            _SECTION_ROOT: crawler.RefreshDoc(document_id="DOC-root"),
            bare: crawler.RefreshDoc(document_id="DOC-bare", source_etag='"v1"', content_hash=stored_hash),
            www: crawler.RefreshDoc(document_id="DOC-www", source_etag='"v1"', content_hash=stored_hash),
        },
    )
    handler = _redirecting_handler(pages, redirects, etags)
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=10),
        root_document_id="DOC-root",
        http_client_factory=_client_factory(handler),
        refresh=state,
        finalize_with_ttl=False,
    )
    assert state.seen_urls == {_SECTION_ROOT, www}
    assert recorder.created_docs == []
    assert [(o, u) for o, u, _ in log.events if u != _SECTION_ROOT] == [("unchanged", www)]


@pytest.mark.asyncio
async def test_refresh_does_not_trust_a_304_earned_by_another_records_etag(recorder: _Recorder):
    # Only the bare URL has a record, and its ETag matches the page it now
    # redirects to. The page is recorded under its final URL, which has no
    # record yet, so it must be fetched and created, not reported unchanged.
    bare, www = "https://example.com/research/a", f"{_WWW}/a"
    pages = {f"{_WWW}/": f'<html><body><a href="{bare}">A</a></body></html>', www: "<html><body>a</body></html>"}
    redirects = {_SECTION_ROOT: f"{_WWW}/", bare: www}
    state, log = _refresh_state(
        recorder,
        {
            _SECTION_ROOT: crawler.RefreshDoc(document_id="DOC-root"),
            bare: crawler.RefreshDoc(document_id="DOC-bare", source_etag='"v1"'),
        },
    )
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=10),
        root_document_id="DOC-root",
        http_client_factory=_client_factory(_redirecting_handler(pages, redirects, {www: '"v1"'})),
        refresh=state,
        finalize_with_ttl=False,
    )
    assert [url for _, url in recorder.created_docs] == [www]
    assert ("created", www) in [(o, u) for o, u, _ in log.events]
    assert bare not in state.seen_urls


@pytest.mark.asyncio
async def test_a_root_that_redirects_to_an_internal_address_fails(recorder: _Recorder):
    pages = {"http://169.254.169.254/latest": "<html><body>credentials</body></html>"}
    redirects = {_SECTION_ROOT: "http://169.254.169.254/latest"}
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=25),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(_redirecting_handler(pages, redirects)),
    )
    assert recorder.s3_puts == []
    assert (recorder.preassigned_root_id, "failed") in recorder.status_updates
    assert recorder.failed_delta == 1


# ── SSRF: nothing non-public is ever requested ─────────────────────────────


_INTERNAL_TARGETS = [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://169.254.170.2/v2/credentials/abc",
    "http://10.0.0.5/admin",
    "http://127.0.0.1:8080/",
    "http://[::1]/",
    "http://[::ffff:169.254.169.254]/latest",
]


def _logging_handler(pages: Dict[str, str], redirects: Dict[str, str]):
    """Like `_redirecting_handler`, but logs every request, robots.txt included."""
    requested: List[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url in redirects:
            return httpx.Response(301, headers={"location": redirects[url]})
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    _handler.requested = requested  # type: ignore[attr-defined]
    return _handler


def _is_internal(url: str) -> bool:
    host = httpx.URL(url).host
    return host.startswith(("169.254.", "10.", "127.")) or ":" in host


@pytest.mark.parametrize("target", _INTERNAL_TARGETS)
@pytest.mark.asyncio
async def test_a_redirect_chain_into_an_internal_address_is_never_requested(
    recorder: _Recorder, target: str
):
    # Two public hops first, so the guard has to hold on every hop, not
    # only on the URL the link named.
    pages = {
        f"{_WWW}/": '<html><body><a href="/research/moved">Moved</a><a href="/research/kept">Kept</a></body></html>',
        f"{_WWW}/kept": "<html><body>kept</body></html>",
        target: "<html><body>credentials</body></html>",
    }
    redirects = {
        _SECTION_ROOT: f"{_WWW}/",
        f"{_WWW}/moved": f"{_WWW}/moved-again",
        f"{_WWW}/moved-again": target,
    }
    handler = _logging_handler(pages, redirects)
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=25),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(handler),
    )
    assert f"{_WWW}/moved-again" in handler.requested
    assert not any(_is_internal(u) for u in handler.requested), handler.requested
    assert [url for _, url in recorder.created_docs] == [f"{_WWW}/kept"]
    assert all(b"credentials" not in body for _, body in recorder.s3_puts)
    # The refused link gave its page slot back and is not a failure.
    assert recorder.discovered_delta == recorder.fetched_delta == 2
    assert recorder.failed_delta == 0
    assert recorder.finalized_status == "complete"


@pytest.mark.parametrize("target", _INTERNAL_TARGETS)
@pytest.mark.asyncio
async def test_a_root_redirecting_internally_fails_without_requesting_it(recorder: _Recorder, target: str):
    handler = _logging_handler({target: "<html><body>credentials</body></html>"}, {_SECTION_ROOT: target})
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=_SECTION_ROOT,
        settings=CrawlSettings(max_depth=1, max_pages=25),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(handler),
    )
    assert not any(_is_internal(u) for u in handler.requested), handler.requested
    assert recorder.s3_puts == []
    assert (recorder.preassigned_root_id, "failed") in recorder.status_updates
    assert recorder.failed_delta == 1


@pytest.mark.asyncio
async def test_a_robots_txt_redirecting_internally_is_never_requested(recorder: _Recorder):
    robots = "https://example.com/robots.txt"
    pages = {"https://example.com/": "<html><body>home</body></html>"}
    handler = _logging_handler(pages, {robots: "http://169.254.169.254/latest/user-data"})
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://example.com/",
        settings=CrawlSettings(max_depth=0, max_pages=1),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(handler),
    )
    assert handler.requested == [robots, "https://example.com/"]
    # Unreachable robots.txt is permissive, as before.
    assert len(recorder.s3_puts) == 1


@pytest.mark.asyncio
async def test_a_normal_multi_hop_redirect_still_lands(recorder: _Recorder):
    # http → https → www → trailing slash: three hops, all public.
    start = "http://example.com/research"
    pages = {f"{_WWW}/": "<html><head><title>Research</title></head><body>research</body></html>"}
    redirects = {
        start: _SECTION_ROOT,
        _SECTION_ROOT: _WWW,
        _WWW: f"{_WWW}/",
    }
    handler = _logging_handler(pages, redirects)
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=start,
        settings=CrawlSettings(max_depth=0, max_pages=1),
        root_document_id=recorder.preassigned_root_id,
        http_client_factory=_client_factory(handler),
    )
    assert [u for u in handler.requested if not u.endswith("robots.txt")] == [
        start,
        _SECTION_ROOT,
        _WWW,
        f"{_WWW}/",
    ]
    assert len(recorder.s3_puts) == 1
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_fetch_page_reports_the_last_hops_url():
    redirects = {_SECTION_ROOT: _WWW, _WWW: f"{_WWW}/"}
    handler = _logging_handler({f"{_WWW}/": "<html><body>x</body></html>"}, redirects)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await crawler._fetch_page(client, _SECTION_ROOT)
    assert page.final_url == f"{_WWW}/"
    assert not page.not_modified


@pytest.mark.parametrize("hops,ok", [(crawler.MAX_REDIRECTS, True), (crawler.MAX_REDIRECTS + 1, False)])
@pytest.mark.asyncio
async def test_the_redirect_hop_cap_holds(hops: int, ok: bool):
    urls = [f"https://example.com/hop/{i}" for i in range(hops + 1)]
    redirects = dict(zip(urls, urls[1:]))
    handler = _logging_handler({urls[-1]: "<html><body>end</body></html>"}, redirects)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if ok:
            page = await crawler._fetch_page(client, urls[0])
            assert page.final_url == urls[-1]
        else:
            with pytest.raises(httpx.TooManyRedirects):
                await crawler._fetch_page(client, urls[0])
    # Never more than the first request plus MAX_REDIRECTS hops.
    assert len(handler.requested) == min(hops, crawler.MAX_REDIRECTS) + 1


@pytest.mark.asyncio
async def test_refresh_304_still_short_circuits_after_a_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SECTION_ROOT:
            return httpx.Response(301, headers={"location": f"{_WWW}/"})
        assert request.headers.get("if-none-match") == '"v1"'
        return httpx.Response(304, headers={"etag": '"v1"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await crawler._fetch_page(client, _SECTION_ROOT, if_none_match='"v1"')
    assert page.not_modified
    assert page.final_url == f"{_WWW}/"


@pytest.mark.asyncio
async def test_a_hostname_resolving_privately_is_refused_by_the_default_client(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
):
    # The production client, end to end: the name looks public, the
    # resolver says 10.x, and no socket is opened.
    import httpcore

    from apis.app_api.web_sources import url_utils

    async def fake_getaddrinfo(host: str, port: int):
        return [(2, 1, 6, "", ("10.0.0.7", port))]

    connects: List[str] = []

    async def spy_connect(self, host: str, port: int, **kwargs):
        connects.append(host)
        raise httpcore.ConnectError("spy")

    monkeypatch.setattr(url_utils, "_getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", spy_connect)
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url="https://intranet.example.com/",
        settings=CrawlSettings(max_depth=1, max_pages=5),
        root_document_id=recorder.preassigned_root_id,
    )
    assert connects == []
    assert (recorder.preassigned_root_id, "failed") in recorder.status_updates
    assert recorder.s3_puts == []


# ── Linked files: PDFs and everything else ─────────────────────────────────


_PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


class _Body:
    """A response body served in chunks, recording how much of it was read."""

    def __init__(self, data: bytes, chunk: int = 64) -> None:
        self.data = data
        self.chunk = chunk
        self.read = 0

    async def __aiter__(self):
        for start in range(0, len(self.data), self.chunk):
            piece = self.data[start : start + self.chunk]
            self.read += len(piece)
            yield piece


def _files_handler(
    files: Dict[str, Tuple[str, bytes | _Body]],
    *,
    redirects: Dict[str, str] | None = None,
    etags: Dict[str, str] | None = None,
):
    """Serve `files` as url -> (content type, body); 301 each `redirects` key.

    A `bytes` body is sent with a Content-Length; a `_Body` is streamed
    without one, the way a chunked response arrives.
    """
    redirects = redirects or {}
    etags = etags or {}
    requested: List[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        requested.append(url)
        if url in redirects:
            return httpx.Response(301, headers={"location": redirects[url]})
        if url not in files:
            return httpx.Response(404, text="not found")
        content_type, body = files[url]
        headers = {"content-type": content_type}
        etag = etags.get(url)
        if etag:
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            headers["etag"] = etag
        return httpx.Response(200, headers=headers, content=body)

    _handler.requested = requested  # type: ignore[attr-defined]
    return _handler


_HTML = "text/html; charset=utf-8"


def _page_linking(*hrefs: str) -> bytes:
    links = "".join(f'<a href="{href}">link</a>' for href in hrefs)
    return f"<html><head><title>Home</title></head><body><p>hi</p>{links}</body></html>".encode()


async def _crawl(rec: _Recorder, handler, *, root: str = ROOT, settings: CrawlSettings | None = None, refresh=None):
    await run_crawl(
        assistant_id="ast-1",
        crawl_id="CRAWL-1",
        user_id="user-1",
        root_url=root,
        settings=settings or CrawlSettings(max_depth=1, max_pages=10),
        root_document_id=rec.preassigned_root_id if refresh is None else "DOC-root",
        http_client_factory=_client_factory(handler),
        refresh=refresh,
        finalize_with_ttl=refresh is None,
    )


@pytest.mark.asyncio
async def test_a_linked_pdf_is_staged_as_a_pdf(recorder: _Recorder):
    pdf_url = "https://example.com/files/FY2025%20Annual%20Report.pdf"
    handler = _files_handler(
        {ROOT: (_HTML, _page_linking("/files/FY2025%20Annual%20Report.pdf")), pdf_url: ("application/pdf", _PDF)}
    )

    await _crawl(recorder, handler)

    assert recorder.finalized_status == "complete"
    assert recorder.fetched_delta == 2 and recorder.failed_delta == 0
    assert not any(status == "failed" for _doc, status in recorder.status_updates)
    [(pdf_doc, source)] = recorder.created_docs
    assert source == pdf_url
    # The bytes go to S3 untouched, as a device upload of a PDF does, and the
    # row says what they are, so the pipeline extracts them as a PDF.
    assert recorder.s3_puts[1] == (
        f"assistants/ast-1/documents/{pdf_doc}/FY2025 Annual Report.pdf",
        _PDF,
    )
    assert recorder.s3_content_types == ["text/markdown", "application/pdf"]
    pdf_meta = next(m for m in recorder.metadata_calls if m["document_id"] == pdf_doc)
    assert pdf_meta == {
        "document_id": pdf_doc,
        "filename": "FY2025 Annual Report.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(_PDF),
    }


@pytest.mark.asyncio
async def test_a_pdf_start_url_is_staged_on_the_routes_document(recorder: _Recorder):
    root = "https://example.com/report.pdf"
    handler = _files_handler({root: ("application/pdf; qs=0.001", _PDF)})

    await _crawl(recorder, handler, root=root)

    assert recorder.created_docs == []
    assert recorder.metadata_calls[0]["document_id"] == recorder.preassigned_root_id
    assert recorder.metadata_calls[0]["content_type"] == "application/pdf"
    assert recorder.s3_puts[0][1] == _PDF
    assert recorder.fetched_delta == 1 and recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_a_pdf_url_served_as_a_generic_binary_is_still_a_pdf(recorder: _Recorder):
    pdf_url = "https://example.com/report.pdf"
    handler = _files_handler(
        {ROOT: (_HTML, _page_linking("/report.pdf")), pdf_url: ("application/octet-stream", _PDF)}
    )

    await _crawl(recorder, handler)

    assert recorder.s3_content_types == ["text/markdown", "application/pdf"]
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_a_response_labeled_pdf_that_is_not_one_fails(recorder: _Recorder):
    pdf_url = "https://example.com/report.pdf"
    handler = _files_handler(
        {ROOT: (_HTML, _page_linking("/report.pdf")), pdf_url: ("application/pdf", b"<html>Login required</html>")}
    )

    await _crawl(recorder, handler)

    [(pdf_doc, _source)] = recorder.created_docs
    assert (pdf_doc, "failed") in recorder.status_updates
    assert recorder.s3_content_types == ["text/markdown"]


@pytest.mark.asyncio
async def test_a_pdf_over_the_crawl_limit_fails_saying_so(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(crawler, "MAX_PDF_BYTES", 1024 * 1024)
    big = _PDF + b"0" * (2 * 1024 * 1024 - len(_PDF))
    pdf_url = "https://example.com/catalog.pdf"
    handler = _files_handler({ROOT: (_HTML, _page_linking("/catalog.pdf")), pdf_url: ("application/pdf", big)})

    await _crawl(recorder, handler)

    [(pdf_doc, _source)] = recorder.created_docs
    assert (pdf_doc, "failed") in recorder.status_updates
    # The row says why, in the sentence every upload refusal uses.
    assert recorder.status_messages[pdf_doc] == "This PDF is too large to add. Files can be up to 1 MB. This one is 2 MB."
    assert recorder.s3_content_types == ["text/markdown"]
    assert recorder.failed_delta == 1


@pytest.mark.asyncio
async def test_a_pdf_without_a_content_length_is_cut_off_at_the_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(crawler, "MAX_PDF_BYTES", 4096)
    body = _Body(_PDF + b"0" * 100_000, chunk=1024)
    handler = _files_handler({"https://example.com/big.pdf": ("application/pdf", body)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(crawler.ResponseTooLarge) as raised:
            await crawler._fetch_page(client, "https://example.com/big.pdf")

    assert raised.value.size_bytes is None
    # Reading stopped at the limit; the rest of the body never arrived.
    assert body.read <= 4096 + 1024


@pytest.mark.asyncio
async def test_a_knowledge_base_file_limit_below_the_crawl_limit_holds(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
):
    # The deployment's per-file limit (upload_limit) binds a crawled PDF as it
    # binds a device upload.
    monkeypatch.setenv("KB_UPLOAD_MAX_FILE_BYTES", str(len(_PDF) - 1))
    pdf_url = "https://example.com/report.pdf"
    handler = _files_handler({ROOT: (_HTML, _page_linking("/report.pdf")), pdf_url: ("application/pdf", _PDF)})

    await _crawl(recorder, handler)

    [(pdf_doc, _source)] = recorder.created_docs
    assert (pdf_doc, "failed") in recorder.status_updates
    assert recorder.status_messages[pdf_doc].startswith("This PDF is too large to add.")
    assert recorder.s3_content_types == ["text/markdown"]


@pytest.mark.asyncio
async def test_a_failed_pdf_row_keeps_the_pdfs_name(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch):
    pdf_url = "https://example.com/report.pdf"
    handler = _files_handler({ROOT: (_HTML, _page_linking("/report.pdf"))})  # the PDF 404s

    created_filenames: List[str] = []
    real_create = crawler.create_document

    async def spy_create(**kwargs):
        created_filenames.append(kwargs["filename"])
        return await real_create(**kwargs)

    monkeypatch.setattr(crawler, "create_document", spy_create)
    await _crawl(recorder, handler)

    assert [u for _id, u in recorder.created_docs] == [pdf_url]
    assert created_filenames == ["report.pdf"]


@pytest.mark.asyncio
async def test_links_to_other_files_never_become_documents(recorder: _Recorder):
    image = _Body(b"\x89PNG" + b"0" * 10_000)
    archive = _Body(b"PK" + b"0" * 10_000)
    handler = _files_handler(
        {
            ROOT: (_HTML, _page_linking("/logo.png", "/data.zip", "/a")),
            "https://example.com/logo.png": ("image/png", image),
            "https://example.com/data.zip": ("application/zip", archive),
            "https://example.com/a": (_HTML, _page_linking()),
        }
    )

    await _crawl(recorder, handler)

    # No red rows: nothing was created for them, nothing failed, and each
    # gave its page-budget slot back.
    assert [u for _id, u in recorder.created_docs] == ["https://example.com/a"]
    assert recorder.failed_delta == 0
    assert recorder.discovered_delta == 2  # the root and /a
    assert recorder.fetched_delta == 2
    # Their bodies were never read.
    assert image.read == 0 and archive.read == 0


@pytest.mark.asyncio
async def test_skipped_files_do_not_spend_the_page_budget(recorder: _Recorder):
    handler = _files_handler(
        {
            ROOT: (_HTML, _page_linking("/one.png", "/two.png", "/a", "/b")),
            "https://example.com/one.png": ("image/png", b"x"),
            "https://example.com/two.png": ("image/png", b"x"),
            "https://example.com/a": (_HTML, _page_linking("/c")),
            "https://example.com/b": (_HTML, _page_linking()),
            "https://example.com/c": (_HTML, _page_linking()),
        }
    )

    # Budget of 4: the root, two images, /a fill it at discovery; the image
    # slots come back, so /c (found on /a) still gets a turn.
    await _crawl(recorder, handler, settings=CrawlSettings(max_depth=2, max_pages=4))

    fetched = {u for _id, u in recorder.created_docs}
    assert "https://example.com/c" in fetched
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_an_unsupported_start_url_says_why_it_is_empty(recorder: _Recorder):
    root = "https://example.com/photo.jpg"
    handler = _files_handler({root: ("image/jpeg", b"\xff\xd8\xff")})

    await _crawl(recorder, handler, root=root)

    assert (recorder.preassigned_root_id, "failed") in recorder.status_updates
    assert recorder.status_messages[recorder.preassigned_root_id] == "This address isn't a web page or a PDF."
    assert recorder.s3_puts == []


@pytest.mark.asyncio
async def test_a_pdf_reached_through_two_urls_gets_one_document(recorder: _Recorder):
    landing = "https://example.com/files/report.pdf"
    handler = _files_handler(
        {ROOT: (_HTML, _page_linking("/report.pdf", "/annual-report")), landing: ("application/pdf", _PDF)},
        redirects={"https://example.com/report.pdf": landing, "https://example.com/annual-report": landing},
    )

    await _crawl(recorder, handler)

    assert [u for _id, u in recorder.created_docs] == [landing]
    assert recorder.s3_content_types.count("application/pdf") == 1
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_a_pdf_outside_the_section_is_never_requested(recorder: _Recorder):
    root = "https://example.com/research/"
    outside = "https://example.com/finance/budget.pdf"
    handler = _files_handler(
        {
            root: (_HTML, _page_linking("/finance/budget.pdf", "/research/plan.pdf")),
            outside: ("application/pdf", _PDF),
            "https://example.com/research/plan.pdf": ("application/pdf", _PDF),
        }
    )

    await _crawl(recorder, handler, root=root)

    assert outside not in handler.requested
    assert [u for _id, u in recorder.created_docs] == ["https://example.com/research/plan.pdf"]


@pytest.mark.asyncio
async def test_a_pdf_redirecting_to_an_internal_address_is_never_requested(recorder: _Recorder):
    handler = _files_handler(
        {ROOT: (_HTML, _page_linking("/report.pdf"))},
        redirects={"https://example.com/report.pdf": "http://169.254.169.254/latest/meta-data/report.pdf"},
    )

    await _crawl(recorder, handler)

    assert not any("169.254" in u for u in handler.requested)
    assert recorder.created_docs == []
    assert recorder.failed_delta == 0


@pytest.mark.asyncio
async def test_refresh_an_identical_pdf_is_not_restaged(recorder: _Recorder):
    pdf_url = "https://example.com/report.pdf"
    page = _page_linking("/report.pdf")
    markdown, _ = crawler._extract_markdown(page.decode(), ROOT)
    state, log = _refresh_state(
        recorder,
        {
            ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash=crawler._content_sha256(markdown)),
            pdf_url: crawler.RefreshDoc(
                document_id="DOC-pdf", source_etag='"old"', content_hash=crawler._bytes_sha256(_PDF)
            ),
        },
    )
    handler = _files_handler({ROOT: (_HTML, page), pdf_url: ("application/pdf", _PDF)}, etags={pdf_url: '"new"'})

    await _crawl(recorder, handler, refresh=state)

    assert state.unchanged == 2 and state.changed == 0 and state.created == 0
    assert recorder.s3_puts == []
    assert pdf_url in state.seen_urls


@pytest.mark.asyncio
async def test_refresh_a_pdf_answering_304_is_unchanged(recorder: _Recorder):
    pdf_url = "https://example.com/report.pdf"
    state, _ = _refresh_state(
        recorder,
        {
            ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash="stale"),
            pdf_url: crawler.RefreshDoc(document_id="DOC-pdf", source_etag='"v1"', content_hash="h"),
        },
    )
    handler = _files_handler(
        {ROOT: (_HTML, _page_linking("/report.pdf")), pdf_url: ("application/pdf", _PDF)}, etags={pdf_url: '"v1"'}
    )

    await _crawl(recorder, handler, refresh=state)

    assert state.unchanged == 1  # the PDF; the root changed
    assert recorder.s3_content_types == ["text/markdown"]
    assert pdf_url in state.seen_urls


@pytest.mark.asyncio
async def test_refresh_a_changed_pdf_emits_before_it_is_restaged(recorder: _Recorder):
    pdf_url = "https://example.com/report.pdf"
    new_pdf = _PDF + b"% revised\n"
    state, log = _refresh_state(
        recorder,
        {pdf_url: crawler.RefreshDoc(document_id="DOC-pdf", content_hash=crawler._bytes_sha256(_PDF), chunk_count=4)},
    )
    handler = _files_handler({ROOT: (_HTML, _page_linking("/report.pdf")), pdf_url: ("application/pdf", new_pdf)})

    await _crawl(recorder, handler, refresh=state)

    pdf_events = [(outcome, puts) for outcome, url, puts in log.events if url == pdf_url]
    pdf_put_index = recorder.s3_content_types.index("application/pdf")
    assert pdf_events == [("changed", pdf_put_index)]
    assert recorder.s3_puts[pdf_put_index][1] == new_pdf


@pytest.mark.asyncio
async def test_refresh_a_known_page_now_serving_a_file_keeps_its_content(recorder: _Recorder):
    # A page the knowledge base already holds that starts serving something
    # else is handled like a failed fetch: still seen, never marked failed.
    known = "https://example.com/a"
    state, _ = _refresh_state(
        recorder,
        {
            ROOT: crawler.RefreshDoc(document_id="DOC-root", content_hash="stale"),
            known: crawler.RefreshDoc(document_id="DOC-a", content_hash="h"),
        },
    )
    handler = _files_handler({ROOT: (_HTML, _page_linking("/a")), known: ("image/png", b"x")})

    await _crawl(recorder, handler, refresh=state)

    assert known in state.seen_urls
    assert ("DOC-a", "failed") not in recorder.status_updates
