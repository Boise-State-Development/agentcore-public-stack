"""HTTP-surface tests for the web-source routes.

These are intentionally narrow: they assert validation, auth, and the
shape of the 202 response. The crawler itself is exercised in
`test_crawler.py`. We patch `asyncio.ensure_future` so the route's
background task is never actually scheduled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.documents.models import Document
from apis.app_api.web_sources import routes as web_routes
from apis.app_api.web_sources.models import CrawlJob, CrawlSettings
from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user_from_session
from tests.routes.conftest import mock_auth_user, mock_no_auth


ASSISTANT_ID = "ast-1"
USER_ID = "user-1"


def _user() -> User:
    return User(
        email="u@example.com",
        user_id=USER_ID,
        name="U",
        roles=["User"],
    )


def _stub_document(document_id: str = "DOC-root00000001") -> Document:
    return Document.model_validate(
        {
            "documentId": document_id,
            "assistantId": ASSISTANT_ID,
            "filename": "page.html",
            "contentType": "text/html",
            "sizeBytes": 0,
            "s3Key": f"assistants/{ASSISTANT_ID}/documents/{document_id}/page.html",
            "status": "uploading",
            "createdAt": "2026-05-23T00:00:00Z",
            "updatedAt": "2026-05-23T00:00:00Z",
        }
    )


def _stub_crawl(crawl_id: str = "CRAWL-1") -> CrawlJob:
    return CrawlJob(
        crawlId=crawl_id,
        assistantId=ASSISTANT_ID,
        rootUrl="https://example.com/",
        status="running",
        settings=CrawlSettings(),
        discoveredCount=0,
        fetchedCount=0,
        failedCount=0,
        startedAt="2026-05-23T00:00:00Z",
        startedByUserId=USER_ID,
    )


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(web_routes.router)
    return _app


class TestStartCrawl:
    def test_returns_202_with_root_document_and_crawl(self, app: FastAPI):
        mock_auth_user(app, _user())
        doc = _stub_document()
        crawl = _stub_crawl()
        run_crawl_mock = AsyncMock(return_value=None)
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value={"assistantId": ASSISTANT_ID},
        ), patch(
            "apis.app_api.web_sources.routes.create_document",
            new_callable=AsyncMock,
            return_value=doc,
        ), patch(
            "apis.app_api.web_sources.routes.create_crawl_job",
            new_callable=AsyncMock,
            return_value=crawl,
        ), patch(
            "apis.app_api.web_sources.routes.run_crawl", run_crawl_mock,
        ), patch(
            "apis.app_api.web_sources.routes.assert_url_is_public",
            return_value="https://example.com/",
        ):
            client = TestClient(app)
            resp = client.post(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawl",
                json={"url": "https://example.com/"},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["crawl"]["crawlId"] == "CRAWL-1"
        assert body["crawl"]["status"] == "running"
        assert len(body["documents"]) == 1
        assert body["documents"][0]["documentId"] == "DOC-root00000001"
        # The route fired run_crawl as a strong-ref'd background task; the
        # mock returned immediately so the coroutine completes without
        # exercising the real crawler.
        run_crawl_mock.assert_called_once()

    def test_returns_404_when_assistant_not_owned(self, app: FastAPI):
        mock_auth_user(app, _user())
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app)
            resp = client.post(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawl",
                json={"url": "https://example.com/"},
            )
        assert resp.status_code == 404

    def test_returns_422_on_invalid_url(self, app: FastAPI):
        mock_auth_user(app, _user())
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value={"assistantId": ASSISTANT_ID},
        ):
            client = TestClient(app)
            # Loopback URL → SSRF guard rejects it.
            resp = client.post(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawl",
                json={"url": "http://127.0.0.1/admin"},
            )
        assert resp.status_code == 422

    def test_returns_422_on_bad_settings_bounds(self, app: FastAPI):
        mock_auth_user(app, _user())
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value={"assistantId": ASSISTANT_ID},
        ):
            client = TestClient(app)
            # max_pages above the cap
            resp = client.post(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawl",
                json={
                    "url": "https://example.com/",
                    "settings": {"maxPages": 9999},
                },
            )
        assert resp.status_code == 422

    def test_returns_401_unauthenticated(self, app: FastAPI):
        mock_no_auth(app)
        client = TestClient(app)
        resp = client.post(
            f"/assistants/{ASSISTANT_ID}/web-sources/crawl",
            json={"url": "https://example.com/"},
        )
        assert resp.status_code == 401


class TestListActiveCrawls:
    def test_returns_active_jobs(self, app: FastAPI):
        mock_auth_user(app, _user())
        crawl = _stub_crawl()
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value={"assistantId": ASSISTANT_ID},
        ), patch(
            "apis.app_api.web_sources.routes.list_active_crawls",
            new_callable=AsyncMock,
            return_value=[crawl],
        ):
            client = TestClient(app)
            resp = client.get(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawls",
                params={"active": "true"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["crawls"]) == 1
        assert body["crawls"][0]["crawlId"] == "CRAWL-1"


class TestGetCrawl:
    def test_returns_single_crawl(self, app: FastAPI):
        mock_auth_user(app, _user())
        crawl = _stub_crawl()
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value={"assistantId": ASSISTANT_ID},
        ), patch(
            "apis.app_api.web_sources.routes.get_crawl_job",
            new_callable=AsyncMock,
            return_value=crawl,
        ):
            client = TestClient(app)
            resp = client.get(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawls/CRAWL-1"
            )
        assert resp.status_code == 200
        assert resp.json()["crawlId"] == "CRAWL-1"

    def test_returns_404_when_missing(self, app: FastAPI):
        mock_auth_user(app, _user())
        with patch(
            "apis.app_api.web_sources.routes.get_assistant",
            new_callable=AsyncMock,
            return_value={"assistantId": ASSISTANT_ID},
        ), patch(
            "apis.app_api.web_sources.routes.get_crawl_job",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app)
            resp = client.get(
                f"/assistants/{ASSISTANT_ID}/web-sources/crawls/CRAWL-X"
            )
        assert resp.status_code == 404
