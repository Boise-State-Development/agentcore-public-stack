"""Tests for refactored delete endpoints (soft-delete + background cleanup).

Endpoints under test:
- DELETE /assistants/{assistant_id}/documents/{document_id} → 204 after soft-delete
- DELETE /assistants/{assistant_id} → 204 after soft-deleting docs + hard-deleting assistant

Requirements: 2.1, 2.2, 8.1, 8.2
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.documents.routes import router as documents_router
from apis.app_api.assistants.routes import router as assistants_router
from apis.app_api.documents.models import Document
from apis.shared.auth.dependencies import get_current_user_id, get_current_user_from_session
from apis.shared.auth.models import User


def _owner_resolve(user_id: str):
    """Build a resolve_assistant_permission return value for an owner."""
    return (SimpleNamespace(owner_id=user_id), "owner")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSISTANT_ID = "ast-001"
USER_ID = "user-001"
DOC_SERVICE = "apis.app_api.documents.services.document_service"
CLEANUP_SERVICE = "apis.app_api.documents.services.cleanup_service"
TEARDOWN = "apis.app_api.kb_migration.teardown"
ASSISTANT_SERVICE = "apis.shared.assistants.service"
SYNC_POLICY_SERVICE = "apis.shared.sync_policies.service"


@pytest.fixture(autouse=True)
def sync_policy_cascades():
    """Both delete endpoints cascade into the sync-policy repository; stub it
    so these route tests stay DynamoDB-free."""
    with patch(
        f"{SYNC_POLICY_SERVICE}.delete_sync_policies_for_source",
        new_callable=AsyncMock,
        return_value=0,
    ) as for_source, patch(
        f"{SYNC_POLICY_SERVICE}.delete_sync_policies_for_assistant",
        new_callable=AsyncMock,
        return_value=0,
    ) as for_assistant:
        yield SimpleNamespace(for_source=for_source, for_assistant=for_assistant)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_document(**overrides) -> Document:
    defaults = dict(
        documentId="doc-001",
        assistantId=ASSISTANT_ID,
        filename="report.pdf",
        contentType="application/pdf",
        sizeBytes=1024,
        s3Key=f"assistants/{ASSISTANT_ID}/documents/doc-001/report.pdf",
        status="deleting",
        chunkCount=5,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        ttl=1737504600,
    )
    defaults.update(overrides)
    return Document.model_validate(defaults)


def _make_user() -> User:
    return User(
        email="test@example.com",
        user_id=USER_ID,
        name="Test User",
        roles=["User"],
    )


# ---------------------------------------------------------------------------
# TestDocumentDeleteEndpoint
# ---------------------------------------------------------------------------


class TestDocumentDeleteEndpoint:
    """DELETE /assistants/{id}/documents/{doc_id} — soft-delete + background cleanup."""

    @pytest.fixture
    def app(self):
        _app = FastAPI()
        _app.include_router(documents_router)
        _app.dependency_overrides[get_current_user_from_session] = _make_user
        return _app

    def test_delete_returns_204_after_soft_delete(self, app):
        """Req 2.1: Endpoint returns 204 after successful soft-delete."""
        doc = _make_document()
        routes_module = "apis.app_api.documents.routes"

        with patch(
            f"{routes_module}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(USER_ID),
        ), patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=doc,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_document_resources",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 204

    def test_delete_cascades_sync_policies_for_document(self, app, sync_policy_cascades):
        """A deleted document must not leave a live sync schedule behind."""
        doc = _make_document()
        routes_module = "apis.app_api.documents.routes"

        with patch(
            f"{routes_module}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(USER_ID),
        ), patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=doc,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_document_resources",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 204
        sync_policy_cascades.for_source.assert_awaited_once_with(ASSISTANT_ID, "doc-001")

    def test_delete_returns_404_when_not_found(self, app):
        """Req 1.5: Returns 404 when soft_delete_document returns None."""
        routes_module = "apis.app_api.documents.routes"
        with patch(
            f"{routes_module}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(USER_ID),
        ), patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 404

    def test_delete_fires_cleanup_in_background(self, app):
        """Req 2.2: Cleanup is scheduled as a background task via asyncio.ensure_future."""
        doc = _make_document()
        routes_module = "apis.app_api.documents.routes"

        with patch(
            f"{routes_module}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(USER_ID),
        ), patch(
            f"{DOC_SERVICE}.soft_delete_document",
            new_callable=AsyncMock,
            return_value=doc,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_document_resources",
            new_callable=AsyncMock,
        ) as mock_cleanup, patch(
            "asyncio.ensure_future",
        ) as mock_ensure:
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}/documents/doc-001")

        assert resp.status_code == 204
        mock_ensure.assert_called_once()


# ---------------------------------------------------------------------------
# TestAssistantDeleteEndpoint
# ---------------------------------------------------------------------------


class TestAssistantDeleteEndpoint:
    """DELETE /assistants/{id} — soft-delete docs, hard-delete assistant, background cleanup."""

    ROUTES_MODULE = "apis.app_api.assistants.routes"

    @pytest.fixture(autouse=True)
    def _deletable(self):
        """Stub the §5.2 listing guard, which runs before any of the work these test.

        The guard reads the Agent to check its listing state, so without this every test
        below would hit a real table. It is stubbed rather than fed a fixture because these
        tests are about delete *orchestration* — what gets cleaned up, in what order — and
        the guard's own rules have a dedicated suite in
        ``tests/shared/test_assistant_delete_listing_guard.py``.
        """
        with patch(
            f"{self.ROUTES_MODULE}.assert_deletable", new_callable=AsyncMock
        ) as guard:
            yield guard

    @pytest.fixture(autouse=True)
    def _teardown(self):
        """Stub queuing the managed knowledge base for teardown, which writes the KB# record.

        Its own behaviour (what it writes, the worker that acts on it) is covered in
        ``tests/lambdas/test_kb_teardown.py``; here only whether and when it is called.
        """
        with patch(f"{TEARDOWN}.queue_teardown", new_callable=AsyncMock, return_value=True) as queue:
            yield queue

    @pytest.fixture
    def app(self):
        _app = FastAPI()
        _app.include_router(assistants_router)
        _app.dependency_overrides[get_current_user_from_session] = _make_user
        return _app

    def test_the_managed_knowledge_base_is_queued_before_anything_is_destroyed(self, app, _teardown):
        """The KB# record and its Bedrock knowledge base used to outlive the agent.

        Queued first so that a failure to queue leaves a delete that can simply be
        retried, rather than an agent already gone whose knowledge base nothing will
        ever find again.
        """
        calls = []
        _teardown.side_effect = lambda *_: calls.append("teardown") or True

        async def _docs(**_):
            calls.append("list")
            return [], None

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents", side_effect=_docs
        ), patch(
            f"{self.ROUTES_MODULE}.delete_assistant", new_callable=AsyncMock, return_value=True
        ), patch("asyncio.ensure_future"):
            resp = TestClient(app).delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        _teardown.assert_awaited_once_with(ASSISTANT_ID)
        assert calls == ["teardown", "list"]

    def test_a_failure_to_queue_the_teardown_fails_the_delete_before_any_damage(self, app, _teardown):
        _teardown.side_effect = RuntimeError("dynamodb unavailable")

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents", new_callable=AsyncMock
        ) as docs, patch(
            f"{self.ROUTES_MODULE}.delete_assistant", new_callable=AsyncMock
        ) as hard_delete:
            resp = TestClient(app).delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 500
        docs.assert_not_awaited()
        hard_delete.assert_not_awaited()

    def test_nothing_is_queued_for_an_agent_that_is_not_the_callers(self, app, _deletable, _teardown):
        """``assert_deletable`` returns None for a missing or someone else's agent; the
        delete then 404s, and nobody else's knowledge base may be queued on the way."""
        _deletable.return_value = None

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=([], None),
        ), patch(f"{self.ROUTES_MODULE}.delete_assistant", new_callable=AsyncMock, return_value=False):
            resp = TestClient(app).delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 404
        _teardown.assert_not_awaited()

    def test_a_listed_agent_is_refused_before_anything_is_cleaned_up(self, app, _deletable, _teardown):
        """⚠️ Ordering, not just refusal.

        Steps 2 and 3 of this handler are destructive. If the guard fired after them, a
        refused delete would leave the Agent gutted *and* still in the store — worse than
        either outcome alone, and exactly what the refusal exists to prevent.
        """
        from apis.shared.assistants.service import AssistantListedError

        _deletable.side_effect = AssistantListedError("still listed")

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents", new_callable=AsyncMock
        ) as docs, patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents", new_callable=AsyncMock
        ) as soft_delete, patch(
            f"{self.ROUTES_MODULE}.delete_assistant", new_callable=AsyncMock
        ) as hard_delete:
            resp = TestClient(app).delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 409
        docs.assert_not_awaited()
        soft_delete.assert_not_awaited()
        hard_delete.assert_not_awaited()
        _teardown.assert_not_awaited()

    def test_delete_soft_deletes_all_docs(self, app):
        """Req 8.1: All documents are batch soft-deleted before assistant is removed."""
        docs = [
            _make_document(documentId="doc-001"),
            _make_document(documentId="doc-002"),
        ]

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(docs, None),
        ), patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents",
            new_callable=AsyncMock,
            return_value=2,
        ) as mock_batch, patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_assistant_documents",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        mock_batch.assert_called_once_with(
            assistant_id=ASSISTANT_ID,
            document_ids=["doc-001", "doc-002"],
        )

    def test_delete_hard_deletes_assistant(self, app):
        """Req 8.1: Assistant record is hard-deleted after soft-deleting docs."""
        docs = [_make_document(documentId="doc-001")]

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(docs, None),
        ), patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents",
            new_callable=AsyncMock,
            return_value=1,
        ), patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_delete_ast, patch(
            f"{CLEANUP_SERVICE}.cleanup_assistant_documents",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        mock_delete_ast.assert_called_once_with(
            assistant_id=ASSISTANT_ID,
            owner_id=USER_ID,
        )

    def test_delete_cascades_sync_policies_for_assistant(self, app, sync_policy_cascades):
        """No sync schedule may outlive its assistant."""
        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=([], None),
        ), patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "asyncio.ensure_future",
        ):
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        sync_policy_cascades.for_assistant.assert_awaited_once_with(ASSISTANT_ID)

    def test_delete_fires_cleanup_in_background(self, app):
        """Req 8.2: Background cleanup is scheduled via asyncio.ensure_future."""
        docs = [_make_document(documentId="doc-001")]

        with patch(
            f"{self.ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(docs, None),
        ), patch(
            f"{DOC_SERVICE}.batch_soft_delete_documents",
            new_callable=AsyncMock,
            return_value=1,
        ), patch(
            f"{self.ROUTES_MODULE}.delete_assistant",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            f"{CLEANUP_SERVICE}.cleanup_assistant_documents",
            new_callable=AsyncMock,
        ), patch(
            "asyncio.ensure_future",
        ) as mock_ensure:
            client = TestClient(app)
            resp = client.delete(f"/assistants/{ASSISTANT_ID}")

        assert resp.status_code == 204
        mock_ensure.assert_called_once()
