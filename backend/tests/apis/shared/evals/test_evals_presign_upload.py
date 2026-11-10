"""The opt-in fixture upload through presign, PUT and complete (unified-attachment-delivery §8.2)."""

from __future__ import annotations

import asyncio

import pytest

from apis.shared.evals.fixtures import FixtureError, upload_via_presign


class _AppApi:
    def __init__(self, presign=(200, None), complete=(200, {"status": "ready"})):
        self.presign = presign
        self.complete = complete
        self.calls = []

    async def call(self, method, path, *, json_body=None, params=None):
        self.calls.append((method, path, json_body))
        if path == "/files/presign":
            status, body = self.presign
            return status, body if body is not None else {
                "uploadId": "u-1", "presignedUrl": "https://bucket.example/put?sig", "expiresAt": "x"}
        return self.complete


def _put(status=200):
    seen = []

    async def put(url, data, headers):
        seen.append((url, data, headers))
        return status

    return put, seen


def test_presign_put_complete_in_order_with_the_signed_content_type():
    app_api, (put, seen) = _AppApi(), _put()
    upload_id = asyncio.run(upload_via_presign(app_api, "eval-s1", "memo.md", "text/markdown", b"# hi", put=put))
    assert upload_id == "u-1"
    assert app_api.calls[0] == ("POST", "/files/presign", {
        "sessionId": "eval-s1", "filename": "memo.md", "mimeType": "text/markdown", "sizeBytes": 4})
    assert seen == [("https://bucket.example/put?sig", b"# hi", {"Content-Type": "text/markdown"})]
    assert app_api.calls[1][:2] == ("POST", "/files/u-1/complete")


def test_admission_refusal_is_a_fixture_error_with_app_apis_reason():
    app_api, (put, seen) = _AppApi(presign=(400, {"detail": "File exceeds 4MB limit"})), _put()
    with pytest.raises(FixtureError, match="presign refused big.md .*400.*4MB"):
        asyncio.run(upload_via_presign(app_api, "eval-s1", "big.md", "text/markdown", b"x" * 10, put=put))
    assert seen == []


def test_a_failed_put_or_complete_stops_the_upload():
    with pytest.raises(FixtureError, match="PUT of a.md"):
        asyncio.run(upload_via_presign(_AppApi(), "s", "a.md", "text/markdown", b"x", put=_put(403)[0]))
    app_api = _AppApi(complete=(409, {"detail": "object missing"}))
    with pytest.raises(FixtureError, match="complete refused a.md: HTTP 409: object missing"):
        asyncio.run(upload_via_presign(app_api, "s", "a.md", "text/markdown", b"x", put=_put()[0]))
