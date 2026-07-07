"""BFS web crawler.

This is the long-running half of the web-source feature. It is spawned via
`asyncio.ensure_future` from the route after the response is already on the
wire — it must never raise out of the top-level `run_crawl` call and must
always finalize the `CrawlJob` it owns. Failure modes:

- transport errors on a page → that one document goes 'failed', crawl
  continues
- robots.txt disallows a page → silently skipped (it never becomes a doc)
- timeout on the whole crawl → caught, crawl marked 'failed'

The hot loop is small on purpose: discover links, gate them through
robots+domain+depth+visited, fetch with per-host jitter, extract markdown,
write to S3. Everything else (status transitions, chunking, embedding) is
the existing documents pipeline.
"""

import asyncio
import logging
import mimetypes
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.robotparser import RobotFileParser

import httpx

from apis.app_api.documents.models import DocumentProvenance
from apis.app_api.documents.services.document_service import (
    create_document,
    update_document_import_metadata,
    update_document_status,
)
from apis.app_api.documents.services.storage_service import (
    _get_s3_key,
    _sanitize_filename,
)
from apis.app_api.web_sources.crawl_repository import (
    finalize_crawl,
    increment_counters,
)
from apis.app_api.web_sources.models import CrawlSettings
from apis.app_api.web_sources.url_utils import (
    absolute_url,
    assert_url_is_public,
    host_of,
    normalize_url,
    same_registrable_domain,
    url_extension_hint,
)

logger = logging.getLogger(__name__)

USER_AGENT = "BoiseStateAI-Assistant-Crawler/1.0 (+contact)"
PER_PAGE_TIMEOUT_SECONDS = 20.0
CRAWL_BUDGET_SECONDS = 15 * 60  # hard ceiling: must outlive any reasonable crawl
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ACCEPT_HEADER = (
    "text/html;q=0.9, application/xhtml+xml;q=0.9, text/plain;q=0.5, */*;q=0.1"
)


class _DocumentCreator:
    """Adapter for the `create_document` call that the route's pre-created
    root document needs to skip — the root already has a record, so the
    crawler must not double-create it.
    """

    def __init__(
        self,
        assistant_id: str,
        user_id: str,
        already_recorded: Dict[str, str],
    ) -> None:
        self.assistant_id = assistant_id
        self.user_id = user_id
        # normalized_url -> document_id
        self.records: Dict[str, str] = dict(already_recorded)

    async def get_or_create(self, normalized_url: str) -> str:
        existing = self.records.get(normalized_url)
        if existing:
            return existing
        provisional_filename = f"{_sanitize_filename(url_extension_hint(normalized_url))}.html"
        document_id = await _create_pending_document(
            assistant_id=self.assistant_id,
            normalized_url=normalized_url,
            user_id=self.user_id,
            filename=provisional_filename,
        )
        self.records[normalized_url] = document_id
        return document_id


async def _create_pending_document(
    *,
    assistant_id: str,
    normalized_url: str,
    user_id: str,
    filename: str,
) -> str:
    """Create a fresh `uploading` document row for a discovered URL."""
    from apis.app_api.documents.services.document_service import _generate_document_id

    document_id = _generate_document_id()
    s3_key = _get_s3_key(assistant_id, document_id, _sanitize_filename(filename))
    await create_document(
        assistant_id=assistant_id,
        filename=filename,
        content_type="text/html",
        size_bytes=0,
        s3_key=s3_key,
        document_id=document_id,
        provenance=DocumentProvenance(
            source_connector_id="web",
            source_adapter_key="http",
            source_file_id=normalized_url,
            imported_by_user_id=user_id,
        ),
    )
    return document_id


# ── KB-sync refresh support ──────────────────────────────────────────────────


@dataclass
class RefreshDoc:
    """What the refresh path needs to know about an existing page document."""

    document_id: str
    source_etag: Optional[str] = None
    content_hash: Optional[str] = None
    chunk_count: int = 0


@dataclass
class RefreshState:
    """Turns a crawl into an upsert-by-URL refresh (docs/specs/assistant-kb-sync.md §6.2).

    `docs` maps normalized URL -> existing document; the crawler reuses
    those records instead of creating duplicates, conditional-GETs with the
    stored ETag, and hash-gates re-staging. `on_result` is the worker's
    hook for all sync-bookkeeping writes (etag/hash/chunk-count stash) —
    the crawler stays ignorant of sync-policy storage.

    Outcomes reported via on_result(url, document_id, outcome, etag, content_hash):
      "unchanged"  — 304 or identical content hash; nothing re-staged
      "changed"    — existing doc, new bytes; emitted BEFORE staging so the
                     worker can stash the previous chunk count first
      "created"    — page new to this crawl; emitted after staging
    `seen_urls` collects every URL that survived the robots gate — the
    worker diffs it against `docs` for miss counting. Fetch failures ARE
    seen (a flaky page is not a missing page); robots-disallowed pages are
    NOT (the site affirmatively told us to stop indexing them).
    """

    docs: Dict[str, RefreshDoc] = field(default_factory=dict)
    on_result: Optional[
        Callable[[str, str, str, Optional[str], Optional[str]], Awaitable[None]]
    ] = None
    seen_urls: Set[str] = field(default_factory=set)
    changed: int = 0
    unchanged: int = 0
    created: int = 0

    async def _emit(
        self,
        url: str,
        document_id: str,
        outcome: str,
        etag: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> None:
        if outcome == "changed":
            self.changed += 1
        elif outcome == "unchanged":
            self.unchanged += 1
        elif outcome == "created":
            self.created += 1
        if self.on_result is not None:
            await self.on_result(url, document_id, outcome, etag, content_hash)


def _content_sha256(markdown: str) -> str:
    import hashlib

    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


# ── robots.txt cache ─────────────────────────────────────────────────────────


class _RobotsCache:
    """Per-crawl robots.txt cache. One fetch per host, lifetime of the job."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: Dict[str, Optional[RobotFileParser]] = {}

    async def allows(self, url: str) -> bool:
        host = host_of(url)
        if not host:
            return False
        parser = self._cache.get(host)
        if host not in self._cache:
            parser = await self._fetch(host, url)
            self._cache[host] = parser
        if parser is None:
            return True  # robots unreachable → permissive (mirrors most crawlers)
        return parser.can_fetch(USER_AGENT, url)

    async def _fetch(self, host: str, sample_url: str) -> Optional[RobotFileParser]:
        # Reuse the original scheme — if the user gave us https://, we ask
        # https://host/robots.txt; same for http.
        from urllib.parse import urlparse

        scheme = urlparse(sample_url).scheme or "https"
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            resp = await self._client.get(
                robots_url,
                follow_redirects=True,
                timeout=PER_PAGE_TIMEOUT_SECONDS,
            )
            if resp.status_code >= 400:
                return None
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser
        except (httpx.HTTPError, ValueError) as e:
            logger.info("robots.txt unreachable for %s (%s); allowing all", host, e)
            return None


# ── HTML extraction ──────────────────────────────────────────────────────────


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _extract_title(html: str) -> Optional[str]:
    match = _TITLE_RE.search(html)
    if not match:
        return None
    title = match.group(1).strip()
    title = re.sub(r"\s+", " ", title)
    return title or None


def _extract_links(html: str, base_url: str) -> List[Tuple[str, str]]:
    """Return (normalized, raw) tuples for every absolute http(s) link in the page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:  # pragma: no cover - dep is in pyproject
        logger.error("beautifulsoup4 is required for web crawling: %s", e)
        return []

    soup = BeautifulSoup(html, "html.parser")
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for anchor in soup.find_all("a", href=True):
        link = anchor.get("href")
        if not link:
            continue
        resolved = absolute_url(base_url, link)
        if resolved is None:
            continue
        normalized, raw = resolved
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append((normalized, raw))
    return out


def _extract_markdown(html: str, url: str) -> Tuple[str, Optional[str]]:
    """Return (markdown, title). Falls back to BS4 text extraction if trafilatura
    is unavailable or returns nothing.
    """
    title = _extract_title(html)
    text: Optional[str] = None
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=False,
            include_images=False,
            include_tables=True,
            favor_recall=True,
        )
    except ImportError:
        logger.debug("trafilatura unavailable, falling back to BS4 text extraction")
    if not text:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n").strip()
        except ImportError:
            text = ""
    if title:
        return f"# {title}\n\n{text or ''}\n", title
    return (text or "") + "\n", None


# ── Per-host delay scheduler ────────────────────────────────────────────────


class _HostDelay:
    """Per-host jittered delay between fetches.

    Tracks the next-allowed-fetch timestamp for each host and sleeps until
    then before yielding. Combined with the global concurrency semaphore
    this gives us "up to N in flight overall, but no more than 1 per host
    per (min..max) seconds" — the polite default.
    """

    def __init__(self, settings: CrawlSettings) -> None:
        self._settings = settings
        self._next_ok: Dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def wait_for(self, url: str) -> None:
        host = host_of(url) or ""
        async with self._lock:
            now = time.monotonic()
            wait_until = self._next_ok[host]
            sleep_for = max(0.0, wait_until - now)
            jitter = random.uniform(
                self._settings.min_delay_seconds, self._settings.max_delay_seconds
            )
            self._next_ok[host] = max(now, wait_until) + jitter
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


# ── Fetch ────────────────────────────────────────────────────────────────────


async def _fetch_page(
    client: httpx.AsyncClient, url: str, if_none_match: Optional[str] = None
) -> Tuple[str, Optional[str], bool]:
    """Fetch a single page. Returns (html, etag, not_modified).

    When `if_none_match` is set (KB-sync refresh with a stored ETag), a 304
    response short-circuits as ("", stored_etag, True) — no body transfer,
    no re-extraction. Raises on non-2xx (other than 304) or non-HTML.
    """
    headers = {"If-None-Match": if_none_match} if if_none_match else None
    resp = await client.get(
        url, follow_redirects=True, timeout=PER_PAGE_TIMEOUT_SECONDS, headers=headers
    )
    if if_none_match and resp.status_code == 304:
        return "", if_none_match, True
    resp.raise_for_status()
    content_type = (resp.headers.get("content-type") or "").lower()
    if not any(
        ct in content_type for ct in ("text/html", "application/xhtml", "text/plain")
    ):
        raise ValueError(
            f"Unsupported content-type for crawl: {content_type or 'unknown'}"
        )
    content = resp.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError(
            f"Response too large: {len(content)} bytes (max {MAX_RESPONSE_BYTES})"
        )
    # Use httpx's encoding inference; surface bytes as text for parsing.
    html = resp.text
    return html, resp.headers.get("etag"), False


# ── S3 stage ────────────────────────────────────────────────────────────────


async def _put_markdown(
    *, assistant_id: str, document_id: str, markdown: str, filename: str
) -> str:
    """PUT extracted markdown into the documents bucket. Returns the final S3 key.

    Triggers the existing S3-event ingestion Lambda which drives the doc
    through chunking/embedding — exactly like a device upload.
    """
    from apis.app_api.documents.services.storage_service import _get_documents_bucket

    import boto3

    sanitized = _sanitize_filename(filename)
    s3_key = _get_s3_key(assistant_id, document_id, sanitized)
    loop = asyncio.get_event_loop()
    s3_client = boto3.client("s3")
    bucket = _get_documents_bucket()
    body = markdown.encode("utf-8")
    await loop.run_in_executor(
        None,
        lambda: s3_client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=body,
            ContentType="text/markdown",
        ),
    )
    return s3_key


# ── Public entry point ──────────────────────────────────────────────────────


async def run_crawl(
    *,
    assistant_id: str,
    crawl_id: str,
    user_id: str,
    root_url: str,
    settings: CrawlSettings,
    root_document_id: str,
    http_client_factory: Optional[
        Callable[[], httpx.AsyncClient]
    ] = None,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    refresh: Optional[RefreshState] = None,
    finalize_with_ttl: bool = True,
    budget_seconds: Optional[int] = None,
) -> None:
    """Run a BFS crawl, staging each fetched page as a Document.

    `root_document_id` was already created synchronously by the route, so
    the crawler reuses it for the root URL and only creates new records for
    pages it discovers later. Never raises.

    The two injection points (`http_client_factory`, `on_progress`) exist
    purely to make unit testing tractable — production passes neither.

    `refresh` switches the crawl into KB-sync upsert mode (see
    RefreshState); `finalize_with_ttl=False` keeps a sync-covered crawl job
    from auto-expiring (see finalize_crawl).
    """

    logger.info(
        "Crawl %s starting (assistant=%s root=%s depth=%d max_pages=%d concurrency=%d)",
        crawl_id,
        assistant_id,
        root_url,
        settings.max_depth,
        settings.max_pages,
        settings.concurrency,
    )

    async def _run() -> None:
        already_recorded = {normalize_url(root_url): root_document_id}
        if refresh is not None:
            # Upsert mode: every known page reuses its existing document
            # record — a re-crawl must never duplicate documents by URL.
            already_recorded.update(
                {url: doc.document_id for url, doc in refresh.docs.items()}
            )
        creator = _DocumentCreator(
            assistant_id=assistant_id,
            user_id=user_id,
            already_recorded=already_recorded,
        )
        await increment_counters(
            assistant_id=assistant_id,
            crawl_id=crawl_id,
            discovered_delta=1,
        )

        visited: Set[str] = set()
        frontier: asyncio.Queue[Tuple[str, int]] = asyncio.Queue()
        await frontier.put((normalize_url(root_url), 0))
        visited.add(normalize_url(root_url))

        semaphore = asyncio.Semaphore(settings.concurrency)
        delay = _HostDelay(settings)
        # Hold a strong reference to every worker task so the event loop's
        # weak-task tracking can't GC them mid-execution (Python docs
        # explicitly warn about this for asyncio.create_task without a
        # held reference).
        worker_tasks: Set[asyncio.Task] = set()
        async with (http_client_factory or _default_client)() as client:
            robots = _RobotsCache(client)
            in_flight = 0
            done_event = asyncio.Event()
            done_event.set()  # starts "done" until we launch the first task

            async def enqueue_links(html: str, url: str, depth: int) -> None:
                if depth >= settings.max_depth:
                    return
                for normalized, _raw in _extract_links(html, url):
                    if normalized in visited:
                        continue
                    if len(visited) >= settings.max_pages:
                        break
                    if settings.same_domain_only and not same_registrable_domain(
                        normalized, root_url
                    ):
                        continue
                    try:
                        assert_url_is_public(normalized, resolve=False)
                    except Exception:
                        continue
                    visited.add(normalized)
                    await increment_counters(
                        assistant_id=assistant_id,
                        crawl_id=crawl_id,
                        discovered_delta=1,
                    )
                    await frontier.put((normalized, depth + 1))

            async def worker(url: str, depth: int) -> None:
                nonlocal in_flight
                try:
                    if not await robots.allows(url):
                        logger.info("robots.txt disallows %s; skipping", url)
                        # No document was created for non-root URLs yet, so
                        # nothing to mark failed. The root URL is the
                        # exception — if the user pointed at a disallowed
                        # root, the route already created a doc; mark it
                        # failed below.
                        if url == normalize_url(root_url):
                            await update_document_status(
                                assistant_id=assistant_id,
                                document_id=root_document_id,
                                status="failed",
                                error_message="The site's robots.txt disallows crawling this URL.",
                            )
                            await increment_counters(
                                assistant_id=assistant_id,
                                crawl_id=crawl_id,
                                failed_delta=1,
                            )
                        return
                    await delay.wait_for(url)
                    existing = refresh.docs.get(url) if refresh is not None else None
                    if refresh is not None:
                        # Seen = survived the robots gate. Fetch failures
                        # still count as seen (transient outage ≠ missing
                        # page); robots-disallowed pages returned above and
                        # do NOT (the site said stop indexing them).
                        refresh.seen_urls.add(url)
                    document_id = await creator.get_or_create(url)
                    logger.info("Crawl %s fetching %s (depth=%d)", crawl_id, url, depth)
                    try:
                        html, etag, not_modified = await _fetch_page(
                            client,
                            url,
                            if_none_match=existing.source_etag if existing else None,
                        )
                    except Exception as fetch_err:
                        logger.warning("Fetch failed for %s: %s", url, fetch_err)
                        if existing is None:
                            # Refresh mode never clobbers an existing indexed
                            # doc's status over a transient fetch error — its
                            # last-good content stays served.
                            await update_document_status(
                                assistant_id=assistant_id,
                                document_id=document_id,
                                status="failed",
                                error_message="The page could not be fetched.",
                                error_details=str(fetch_err)[:500],
                            )
                        await increment_counters(
                            assistant_id=assistant_id,
                            crawl_id=crawl_id,
                            failed_delta=1,
                        )
                        return

                    if not_modified:
                        await refresh._emit(url, document_id, "unchanged", etag)
                        await increment_counters(
                            assistant_id=assistant_id,
                            crawl_id=crawl_id,
                            fetched_delta=1,
                        )
                        # 304 carries no body, so no links to walk from this
                        # page this run — its previously-discovered children
                        # are still in refresh.docs and get their own fetches
                        # only if some other page still links to them; the
                        # miss counter (not this run) decides their fate.
                        return

                    markdown, title = _extract_markdown(html, url)
                    if not markdown.strip():
                        if existing is None:
                            await update_document_status(
                                assistant_id=assistant_id,
                                document_id=document_id,
                                status="failed",
                                error_message="The page had no extractable content.",
                            )
                        await increment_counters(
                            assistant_id=assistant_id,
                            crawl_id=crawl_id,
                            failed_delta=1,
                        )
                        return

                    if refresh is not None:
                        content_hash = _content_sha256(markdown)
                        if existing is not None:
                            if existing.content_hash and existing.content_hash == content_hash:
                                # Same bytes under a new/absent ETag: record
                                # the fresh etag so gate 1 works next run,
                                # skip the re-embed entirely.
                                await refresh._emit(url, document_id, "unchanged", etag, content_hash)
                                await increment_counters(
                                    assistant_id=assistant_id,
                                    crawl_id=crawl_id,
                                    fetched_delta=1,
                                )
                                await enqueue_links(html, url, depth)
                                return
                            # Changed: let the worker stash the previous
                            # chunk count BEFORE the S3 overwrite fires the
                            # ingestion event.
                            await refresh._emit(url, document_id, "changed", etag, content_hash)
                        else:
                            await refresh._emit(url, document_id, "created", etag, content_hash)
                    display_name = (
                        title or url_extension_hint(url)
                    ).strip() or "page"
                    filename = f"{display_name}.md"
                    logger.info(
                        "Crawl %s staging %s -> s3 (%d bytes markdown)",
                        crawl_id,
                        url,
                        len(markdown.encode("utf-8")),
                    )
                    s3_key = await _put_markdown(
                        assistant_id=assistant_id,
                        document_id=document_id,
                        markdown=markdown,
                        filename=filename,
                    )
                    await update_document_import_metadata(
                        assistant_id=assistant_id,
                        document_id=document_id,
                        filename=filename,
                        content_type="text/markdown",
                        size_bytes=len(markdown.encode("utf-8")),
                        s3_key=s3_key,
                        source_etag=etag,
                    )
                    logger.info(
                        "Crawl %s wrote %s to s3 (key=%s); ingestion Lambda will take over",
                        crawl_id,
                        url,
                        s3_key,
                    )
                    await increment_counters(
                        assistant_id=assistant_id,
                        crawl_id=crawl_id,
                        fetched_delta=1,
                    )
                    if on_progress is not None:
                        await on_progress(url)

                    await enqueue_links(html, url, depth)
                finally:
                    in_flight -= 1
                    if in_flight == 0 and frontier.empty():
                        done_event.set()

            async def scheduler() -> None:
                nonlocal in_flight
                while True:
                    if frontier.empty():
                        if in_flight == 0:
                            return
                        await asyncio.sleep(0.05)
                        continue
                    url, depth = await frontier.get()
                    await semaphore.acquire()
                    in_flight += 1
                    done_event.clear()

                    async def _wrapped(u: str = url, d: int = depth) -> None:
                        try:
                            await worker(u, d)
                        finally:
                            semaphore.release()

                    task = asyncio.create_task(_wrapped())
                    worker_tasks.add(task)
                    task.add_done_callback(worker_tasks.discard)

            await scheduler()
            await done_event.wait()

    error: Optional[str] = None
    status: str = "complete"
    try:
        await asyncio.wait_for(_run(), timeout=budget_seconds or CRAWL_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Crawl %s exceeded budget; marking failed", crawl_id)
        error = "Crawl exceeded the time budget."
        status = "failed"
    except Exception as e:
        logger.exception("Crawl %s failed: %s", crawl_id, e)
        error = str(e)[:500]
        status = "failed"
    finally:
        logger.info("Crawl %s finalizing with status=%s", crawl_id, status)
        await finalize_crawl(
            assistant_id=assistant_id,
            crawl_id=crawl_id,
            status=status,  # type: ignore[arg-type]
            error=error,
            set_ttl=finalize_with_ttl,
        )


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER},
        timeout=PER_PAGE_TIMEOUT_SECONDS,
    )


# Silence unused-import lint for mimetypes; it's reserved for filename hints
# in future cases (e.g. `.txt` URLs) and stays here for parity with the
# documents pipeline.
_ = mimetypes
