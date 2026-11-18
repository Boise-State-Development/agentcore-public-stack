"""GET /assistants/{id}/documents/{doc}/content — the citation card's content view.

Anyone the chat path admits may read a cited document's indexed text, bounded by the
#111 floor (show_citations AND allow_document_download), complete documents only and
text-like types only. The route functions are called directly with the assistant,
document and S3 helpers monkeypatched, so no app or AWS is needed.
"""

import io
import types

import pytest
from fastapi import HTTPException

import apis.app_api.documents.routes as routes
from apis.app_api.documents.services.storage_service import read_text_object
from apis.shared.blocking_io import run_blocking_io_inline


class _User:
    user_id = "viewer-1"
    email = "viewer@example.com"


def _assistant(show: bool = True, allow: bool = True):
    return types.SimpleNamespace(owner_id="owner-1", show_citations=show, allow_document_download=allow)


def _doc(**overrides):
    fields = dict(
        s3_key="assistants/a/documents/d/home.md",
        filename="Home - Example.md",
        content_type="text/markdown",
        status="complete",
        source_connector_id="web",
        source_file_id="https://example.edu/home",
    )
    fields.update(overrides)
    doc = types.SimpleNamespace(**fields)
    doc.model_dump = lambda by_alias=False: {
        "sourceConnectorId": doc.source_connector_id,
        "sourceFileId": doc.source_file_id,
    }
    return doc


class _Body:
    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)
        self.closed = False

    def read(self, n: int) -> bytes:
        return self._stream.read(n)

    def close(self) -> None:
        self.closed = True


class _S3:
    def __init__(self, data: bytes):
        self.data = data
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"Body": _Body(self.data)}


@pytest.fixture(autouse=True)
def _inline_blocking_io():
    run_blocking_io_inline(True)
    yield
    run_blocking_io_inline(False)


def _wire(monkeypatch, *, assistant=None, permission="viewer", document=None, s3=None):
    seen = {}

    async def _access(**kwargs):
        seen["access"] = kwargs
        return (assistant, permission) if assistant else (None, None)

    async def _getdoc(assistant_id, document_id, owner_id):
        seen["owner_id"] = owner_id
        return document

    monkeypatch.setattr(routes, "get_assistant_with_access_check", _access)
    monkeypatch.setattr(routes, "get_document_service", _getdoc)
    monkeypatch.setattr(routes, "content_s3_client", lambda: s3 or _S3(b""))
    monkeypatch.setattr(routes, "_get_documents_bucket", lambda: "docs-bucket")

    async def _no_edit_gate(*_a, **_k):
        raise AssertionError("the content view must not require owner|editor")

    monkeypatch.setattr(routes, "_require_edit_permission", _no_edit_gate)
    monkeypatch.setattr(routes, "resolve_assistant_permission", _no_edit_gate)
    return seen


async def _call():
    return await routes.get_document_content(assistant_id="a", document_id="d", current_user=_User())


@pytest.mark.asyncio
async def test_viewer_reads_a_crawled_page(monkeypatch):
    s3 = _S3("# Home\n\nWelcome — café".encode("utf-8"))
    seen = _wire(monkeypatch, assistant=_assistant(), document=_doc(), s3=s3)

    resp = await _call()

    assert resp.text == "# Home\n\nWelcome — café"
    assert resp.truncated is False
    assert resp.content_type == "text/markdown"
    assert resp.filename == "Home - Example.md"
    assert resp.source_url == "https://example.edu/home"
    assert seen["owner_id"] == "owner-1"
    assert s3.calls == [
        {"Bucket": "docs-bucket", "Key": "assistants/a/documents/d/home.md", "Range": f"bytes=0-{routes.DOCUMENT_CONTENT_MAX_BYTES}"}
    ]
    dumped = resp.model_dump(by_alias=True)
    # Bounded text and the page address, never a presigned URL for the object.
    assert set(dumped) == {"filename", "sourceUrl", "contentType", "text", "truncated"}


@pytest.mark.asyncio
async def test_no_access_reads_as_not_found(monkeypatch):
    _wire(monkeypatch, assistant=None, document=_doc())

    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("show,allow", [(True, False), (False, True), (False, False)])
async def test_download_floor_refuses(monkeypatch, show, allow):
    async def _getdoc(*_a, **_k):
        raise AssertionError("document must not be fetched once the floor blocks the request")

    _wire(monkeypatch, assistant=_assistant(show, allow), document=_doc())
    monkeypatch.setattr(routes, "get_document_service", _getdoc)

    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 403
    assert "turned off" in str(exc.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("doc_status,code", [("deleting", 404), ("embedding", 409), ("failed", 409), ("provisioning", 409)])
async def test_only_complete_documents(monkeypatch, doc_status, code):
    _wire(monkeypatch, assistant=_assistant(), document=_doc(status=doc_status), s3=_S3(b"never read"))

    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == code


@pytest.mark.asyncio
async def test_missing_document_is_not_found(monkeypatch):
    _wire(monkeypatch, assistant=_assistant(), document=None)

    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,filename",
    [("application/pdf", "report.pdf"), ("text/html", "page.md"), ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"), ("", "image.png")],
)
async def test_other_types_are_refused(monkeypatch, content_type, filename):
    s3 = _S3(b"%PDF")
    _wire(monkeypatch, assistant=_assistant(), document=_doc(content_type=content_type, filename=filename), s3=s3)

    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 415
    assert exc.value.detail == routes.CONTENT_NOT_VIEWABLE_DETAIL
    assert s3.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,filename,expected",
    [
        ("text/plain; charset=utf-8", "notes.txt", "text/plain"),
        ("text/x-markdown", "a.md", "text/markdown"),
        ("application/octet-stream", "README.MD", "text/markdown"),
        ("", "notes.txt", "text/plain"),
    ],
)
async def test_text_types_and_generic_extension_fallback(monkeypatch, content_type, filename, expected):
    doc = _doc(content_type=content_type, filename=filename, source_connector_id=None, source_file_id=None)
    _wire(monkeypatch, assistant=_assistant(), document=doc, s3=_S3(b"hello"))

    resp = await _call()
    assert resp.content_type == expected
    assert resp.source_url is None


@pytest.mark.asyncio
async def test_drive_import_is_not_given_a_source_url(monkeypatch):
    doc = _doc(source_connector_id="google-drive", source_file_id="1AbCdEf")
    _wire(monkeypatch, assistant=_assistant(), document=doc, s3=_S3(b"hi"))

    resp = await _call()
    assert resp.source_url is None


@pytest.mark.asyncio
async def test_storage_failure_is_a_generic_500(monkeypatch):
    class _Broken:
        def get_object(self, **_k):
            raise RuntimeError("AccessDenied arn:aws:s3:::docs-bucket/secret")

    _wire(monkeypatch, assistant=_assistant(), document=_doc(), s3=_Broken())

    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 500
    assert "arn:" not in str(exc.value.detail)


# ── read_text_object: the byte cap ───────────────────────────────────────────


def test_under_the_cap_is_whole():
    body_holder = {}

    def _get(**kw):
        body_holder["body"] = _Body(b"abc")
        return {"Body": body_holder["body"]}

    assert read_text_object(_get, "b", "k", 10) == ("abc", False)
    assert body_holder["body"].closed


def test_exactly_the_cap_is_not_truncated():
    s3 = _S3(b"abcd")
    assert read_text_object(s3.get_object, "b", "k", 4) == ("abcd", False)
    assert s3.calls[0]["Range"] == "bytes=0-4"


def test_over_the_cap_is_cut_and_flagged():
    s3 = _S3(b"abcdefgh")
    assert read_text_object(s3.get_object, "b", "k", 4) == ("abcd", True)


def test_a_character_split_by_the_cap_is_dropped():
    # "é" is two bytes; a cap of 2 lands inside it.
    s3 = _S3("aé!".encode("utf-8"))
    text, truncated = read_text_object(s3.get_object, "b", "k", 2)
    assert (text, truncated) == ("a", True)


def test_invalid_bytes_are_replaced_not_raised():
    s3 = _S3(b"ok \xff end")
    text, truncated = read_text_object(s3.get_object, "b", "k", 100)
    assert text == "ok � end"
    assert truncated is False
