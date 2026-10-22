"""The chunk inspector: show an owner what the knowledge base actually extracted.

Feature: `kb-chunk-inspector`. Tooling half of the task-16.2 decision on
`managed-kb-migration` §5.41.

The failure this endpoint exists to make visible is not a crash — it is a *confident
wrong answer*. The managed backend flattens a column-structured diagram at ingestion,
so "how many credits in semester 4" comes back plausible and wrong with no trace. 16.2
decided against fixing the parser and in favour of user guidance; guidance the user
cannot verify is not guidance, so this endpoint is what lets them look.

Two things here are security properties rather than features, and both are mutation-
guarded below:

**The document filter is the isolation boundary.** It must be `equals` on
`document_id`. A prefix or substring operator matching `DOC-1` also admits `DOC-10`,
which means one owner's inspector renders another document's content. That is a leak,
not a display bug — hence `ISOLATION_SAFE_FILTER_OPERATORS`.

**A classic knowledge base must refuse rather than approximate.** The legacy adapter
accepts no filter and ignores `top_k`; it always returns five results from the *whole*
knowledge base. Running it anyway would show the owner other documents' chunks under
the heading of theirs.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.documents.routes import router
from apis.app_api.documents.services import chunk_inspector as ci
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.kb_backend.managed_backend import ISOLATION_SAFE_FILTER_OPERATORS
from apis.shared.kb_backend.protocol import Chunk

ROUTES_MODULE = "apis.app_api.documents.routes"
INSPECTOR_MODULE = "apis.app_api.documents.services.chunk_inspector"
RESOLVER_MODULE = "apis.shared.kb_backend.resolver"
ASSISTANT_ID = "ast-insp01"
DOCUMENT_ID = "DOC-insp01"
OTHER_DOCUMENT_ID = "DOC-insp01-other"
USER_ID = "user-insp"
FILENAME = "4-yr-flowchart-v2026.pdf"


@pytest.fixture()
def app():
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[get_current_user_from_session] = lambda: User(
        user_id=USER_ID, email=f"{USER_ID}@example.com", name="Test User", roles=["User"]
    )
    return _app


def _owner():
    return (SimpleNamespace(owner_id=USER_ID), "owner")


def _viewer():
    return (SimpleNamespace(owner_id="somebody-else"), "viewer")


def _document(status="complete", filename=FILENAME):
    return SimpleNamespace(
        document_id=DOCUMENT_ID, filename=filename, status=status, s3_key="k"
    )


def _chunk(text, document_id=DOCUMENT_ID, relevance=0.5, metadata=None):
    return Chunk(
        text=text,
        relevance=relevance,
        document_id=document_id,
        metadata=metadata or {},
        key=f"{document_id}#0",
    )


class _FakeBackend:
    """Records the retrieval filter it was handed and returns canned chunks.

    Modelling the filter is the whole point: the isolation guarantee lives in the
    argument, not in the response, so a fake that ignored it could not tell a scoped
    query from an unscoped one.

    ``honour_filter=False`` models a backend that accepts the filter and does not
    apply it — a service-side regression, or an adapter that drops the argument. That
    is not hypothetical enough to skip: it is the failure the post-filter exists for,
    and it is silent.
    """

    def __init__(self, chunks, honour_filter=True):
        self._chunks = chunks
        self._honour_filter = honour_filter
        self.calls = []

    async def search(self, kb_ref, query, top_k=5, retrieval_filter=None):
        self.calls.append(
            {"kb_ref": kb_ref, "query": query, "top_k": top_k, "filter": retrieval_filter}
        )
        if not retrieval_filter or not self._honour_filter:
            # Model the real world: unfiltered, the index returns everything it has,
            # including other documents. A fake that returned only the "right" chunks
            # would hide exactly the bug being guarded against.
            return self._chunks
        wanted = retrieval_filter.get("equals", {}).get("value")
        return [c for c in self._chunks if c.document_id == wanted]


def _get(app):
    return TestClient(app).get(
        f"/assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/chunks"
    )


def _inspect(app, *, document, engine="managed", backend=None, record=None):
    """Drive the endpoint with the resolver and document lookup stubbed."""
    return (
        patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner(),
        ),
        patch(
            f"{ROUTES_MODULE}.get_document_service",
            new_callable=AsyncMock,
            return_value=document,
        ),
        # Patched on the resolver module, not on the inspector: the inspector imports
        # it function-locally (stdlib-only module scope is the house rule for anything
        # the size-constrained Lambda images touch), so there is no module attribute
        # to patch on this side.
        patch(f"{RESOLVER_MODULE}.load_record", return_value=record or {}),
        patch(f"{RESOLVER_MODULE}.resolve_engine_for", return_value=engine),
        patch(f"{RESOLVER_MODULE}.resolve_backend", return_value=backend),
    )


class TestTheHappyPath:
    def test_an_owner_sees_the_full_extracted_text(self, app):
        backend = _FakeBackend(
            [
                _chunk("Semester 1 | ENGL 101 | 3 credits", metadata={"page": "1"}),
                _chunk("x" * 900),  # longer than the 500-char citation cap
            ]
        )
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            resp = _get(app)

        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["engine"] == "managed"
        assert body["returned"] == 2
        assert body["capReached"] is False
        # NOT truncated to 500 — the whole reason the citation trace cannot serve this.
        assert len(body["chunks"][1]["text"]) == 900
        assert body["chunks"][0]["page"] == 1

    def test_the_filter_sent_to_the_backend_scopes_to_one_document(self, app):
        """MUTATION GUARD: drop `retrieval_filter=document_filter(...)` from
        `inspect_document_chunks` and this fails.

        This is the guard for the filter itself, asserted on the ARGUMENT rather than
        on the response, and that distinction was earned the hard way. The obvious
        guard — seed a second document's chunks and assert they do not appear — passes
        with the filter removed, because the post-filter in `inspect_document_chunks`
        catches them on the way out. A test that green-lights the mutated code is not
        a guard, however sensible it reads. See
        `test_a_backend_that_ignores_the_filter_still_cannot_leak` for the other layer.
        """
        backend = _FakeBackend([_chunk("only mine")])
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            _get(app)

        call = backend.calls[0]
        assert call["query"] == FILENAME
        assert call["top_k"] == ci.INSPECT_TOP_K
        assert call["filter"] == {
            "equals": {"key": "document_id", "value": DOCUMENT_ID}
        }

    def test_repeated_passages_are_collapsed(self, app):
        """`Retrieve` is query-ranked, not a cursor, so it can return a passage twice.
        Showing a mangled table three times would read as three bad ingestions."""
        backend = _FakeBackend([_chunk("same text"), _chunk("same text"), _chunk("other")])
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            body = _get(app).json()

        assert [c["text"] for c in body["chunks"]] == ["same text", "other"]

    def test_cap_reached_is_reported_when_the_ceiling_is_hit(self, app):
        """Honesty, not paging (Req 3). Bedrock has no chunk-enumeration API, so the
        UI must say 'up to N' rather than 'this document has N chunks'."""
        backend = _FakeBackend([_chunk(f"chunk {i}") for i in range(ci.INSPECT_TOP_K)])
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            body = _get(app).json()

        assert body["capReached"] is True
        assert body["returned"] == ci.INSPECT_TOP_K

    def test_a_page_number_is_never_invented(self, app):
        """A fabricated page would be indistinguishable from a real one and would make
        an unordered set look authoritatively ordered."""
        backend = _FakeBackend([_chunk("no page metadata", metadata={"junk": "x"})])
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            body = _get(app).json()

        assert body["chunks"][0]["page"] is None


class TestIsolation:
    def test_a_backend_that_ignores_the_filter_still_cannot_leak(self, app):
        """MUTATION GUARD for the SECOND layer: delete the `scoped = [...]` post-filter
        in `inspect_document_chunks` and this fails.

        The fake here deliberately ignores the filter it is handed, modelling a backend
        that stops honouring it — a service-side regression, or a future adapter that
        accepts the argument and drops it. Neither would raise. The response would just
        quietly start carrying another document's content under this document's
        filename, and nothing else in the system would notice.

        Belt and braces is the right call here precisely because the failure is silent
        and the blast radius is one owner reading another's document.
        """
        backend = _FakeBackend(
            [
                _chunk("mine", document_id=DOCUMENT_ID),
                _chunk("SOMEBODY ELSE'S CONTENT", document_id=OTHER_DOCUMENT_ID),
            ],
            honour_filter=False,
        )
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            body = _get(app).json()

        texts = [c["text"] for c in body["chunks"]]
        assert texts == ["mine"]
        assert "SOMEBODY ELSE'S CONTENT" not in texts

    def test_only_the_requested_document_reaches_the_owner(self, app):
        """The observable guarantee, through whichever layer delivers it. Both the
        filter and the post-filter would have to fail for this to break."""
        backend = _FakeBackend(
            [
                _chunk("mine", document_id=DOCUMENT_ID),
                _chunk("SOMEBODY ELSE'S CONTENT", document_id=OTHER_DOCUMENT_ID),
            ]
        )
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            body = _get(app).json()

        assert [c["text"] for c in body["chunks"]] == ["mine"]

    def test_the_filter_operator_is_isolation_safe(self):
        """The operator choice IS the isolation boundary. A prefix match for `DOC-1`
        admits `DOC-10`, so this asserts the operator rather than trusting it."""
        built = ci.document_filter(DOCUMENT_ID)
        assert list(built) == ["equals"]
        assert set(built) <= ISOLATION_SAFE_FILTER_OPERATORS
        assert built["equals"] == {"key": "document_id", "value": DOCUMENT_ID}

    def test_a_prefix_style_document_id_cannot_over_match(self, app):
        """`DOC-insp01` must not admit `DOC-insp01-other`, which is exactly what a
        substring operator would do. Driven through a filter-honouring fake so this
        exercises the real `equals` semantics rather than the post-filter."""
        backend = _FakeBackend(
            [
                _chunk("mine", document_id=DOCUMENT_ID),
                _chunk("longer id, must not match", document_id=OTHER_DOCUMENT_ID),
            ]
        )
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            body = _get(app).json()

        assert [c["text"] for c in body["chunks"]] == ["mine"]

    def test_a_viewer_is_refused(self, app):
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_viewer(),
        ):
            resp = _get(app)
        assert resp.status_code == 403


class TestWhatCannotBeInspected:
    def test_a_classic_knowledge_base_refuses_rather_than_approximating(self, app):
        """MUTATION GUARD: let the legacy engine through to `search` and this fails.
        The legacy adapter takes no filter and ignores top_k — it returns five results
        from the WHOLE knowledge base — so 'approximating' means rendering other
        documents' content under this document's name."""
        backend = _FakeBackend([_chunk("whole-KB result", document_id=OTHER_DOCUMENT_ID)])
        p, d, lr, re_, rb = _inspect(
            app, document=_document(), engine="s3vectors", backend=backend
        )
        with p, d, lr, re_, rb:
            resp = _get(app)

        assert resp.status_code == 200  # an answer, not an error
        body = resp.json()
        assert body["available"] is False
        assert body["chunks"] == []
        assert "classic" in body["reason"].lower()
        # The decisive assertion: the backend was never asked.
        assert backend.calls == []

    @pytest.mark.parametrize(
        "status", ["uploading", "chunking", "embedding", "provisioning"]
    )
    def test_a_document_still_processing_is_409_not_404(self, app, status):
        """It exists; it simply has no content yet. A 404 would tell the owner their
        file is missing, which is both wrong and alarming."""
        p, d, lr, re_, rb = _inspect(app, document=_document(status=status))
        with p, d, lr, re_, rb:
            resp = _get(app)

        assert resp.status_code == 409

    def test_provisioning_says_the_knowledge_base_is_being_created(self, app):
        """Born-managed's leading status. 'Still processing' would understate it — the
        wait is on the knowledge base itself, not on this file."""
        p, d, lr, re_, rb = _inspect(app, document=_document(status="provisioning"))
        with p, d, lr, re_, rb:
            detail = _get(app).json()["detail"]

        assert "knowledge base" in detail.lower()
        assert "being created" in detail.lower()

    def test_a_failed_document_explains_itself(self, app):
        p, d, lr, re_, rb = _inspect(app, document=_document(status="failed"))
        with p, d, lr, re_, rb:
            resp = _get(app)

        assert resp.status_code == 409
        assert "could not be processed" in resp.json()["detail"].lower()

    def test_a_missing_document_is_404(self, app):
        p, d, lr, re_, rb = _inspect(app, document=None)
        with p, d, lr, re_, rb:
            resp = _get(app)
        assert resp.status_code == 404

    def test_a_soft_deleted_document_is_404_not_its_content(self, app):
        """`deleting` means removal is under way on purpose. Rendering its content is
        resurrecting it in the one place the user was told it is gone."""
        p, d, lr, re_, rb = _inspect(app, document=_document(status="deleting"))
        with p, d, lr, re_, rb:
            resp = _get(app)
        assert resp.status_code == 404


class TestBoundedCost:
    def test_exactly_one_retrieve_per_request(self, app):
        """Req 6. `Retrieve` was measured at 662–695 ms p50 and offers no cursor, so a
        second call would cost that again for an overlapping arbitrary subset."""
        backend = _FakeBackend([_chunk("a"), _chunk("b")])
        p, d, lr, re_, rb = _inspect(app, document=_document(), backend=backend)
        with p, d, lr, re_, rb:
            _get(app)

        assert len(backend.calls) == 1
