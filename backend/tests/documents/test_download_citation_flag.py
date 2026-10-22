"""Issue #111 — server-side document-download floor on GET /{document_id}/download.

Downloads are allowed only when BOTH show_citations and allow_document_download are
on (citations off implies downloads off). These call the route function directly with
the assistant/document/presign helpers monkeypatched, so no app or AWS is needed.
"""

import types

import pytest
from fastapi import HTTPException

import apis.app_api.documents.routes as routes


class _User:
    user_id = "u1"
    email = "alice@example.com"


def _assistant(show: bool, allow: bool):
    return types.SimpleNamespace(owner_id="u1", show_citations=show, allow_document_download=allow)


def _doc():
    return types.SimpleNamespace(s3_key="assistants/a/documents/d.pdf", filename="d.pdf")


@pytest.mark.asyncio
async def test_download_allowed_when_both_flags_on(monkeypatch):
    async def _perm(**_kw):
        return (_assistant(True, True), "owner")

    async def _getdoc(*_a, **_k):
        return _doc()

    async def _url(**_k):
        return "https://signed.example/doc"

    monkeypatch.setattr(routes, "resolve_assistant_permission", _perm)
    monkeypatch.setattr(routes, "get_document_service", _getdoc)
    monkeypatch.setattr(routes, "generate_download_url", _url)

    resp = await routes.get_download_url(assistant_id="a", document_id="d", current_user=_User())
    assert resp.filename == "d.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "show,allow",
    [(True, False), (False, True), (False, False)],
)
async def test_download_403_when_disabled(monkeypatch, show, allow):
    async def _perm(**_kw):
        return (_assistant(show, allow), "owner")

    async def _getdoc(*_a, **_k):
        raise AssertionError("document must not be fetched once the floor blocks the request")

    monkeypatch.setattr(routes, "resolve_assistant_permission", _perm)
    monkeypatch.setattr(routes, "get_document_service", _getdoc)

    with pytest.raises(HTTPException) as exc:
        await routes.get_download_url(assistant_id="a", document_id="d", current_user=_User())
    assert exc.value.status_code == 403
    assert "disabled" in str(exc.value.detail).lower()
