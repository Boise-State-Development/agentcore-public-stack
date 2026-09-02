"""Regression tests for the skill-resource stored-XSS chain.

The bug: **an unprivileged author could plant a script that executed with a
system_admin's session on the SPA's own origin.** Two failures chained.

1. *Write side.* ``POST /skills/mine/{id}/resources`` is reachable by any
   authenticated user and persisted the **client-supplied multipart
   Content-Type verbatim** with no allowlist, so an ``.html`` file could be
   stored as ``text/html``.
2. *Read side.* The read routes reflected that stored type as the HTTP response
   media type together with ``Content-Disposition: inline``. app-api shares an
   origin with the Angular SPA (the CloudFront ``/api/*`` behavior), so the
   uploaded file parsed as a **top-level HTML document on the SPA's origin** and
   its inline ``<script>`` ran with the viewer's session — reading the
   non-httpOnly CSRF cookie and driving authenticated admin calls.

Both halves are pinned here, plus the case that matters most operationally: a
row written *before* the allowlist existed (``content_type == "text/html"``)
must be neutralized on the read path, because the fix ships without a data
migration.
"""

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.skills import routes as admin_skill_routes
from apis.app_api.skills import routes as my_skill_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.skills.resource_types import (
    SAFE_EXTENSION_CONTENT_TYPES,
    SkillResourceTypeError,
    resolve_upload_content_type,
    resource_download_headers,
    safe_download_content_type,
)
from tests.conftest import override_admin_auth

from .conftest import AWS_REGION, SKILL_RESOURCES_BUCKET

# The payload from the report, verbatim in shape.
XSS_PAYLOAD = (
    b"<html><body><h1>XSS Verify</h1><script>"
    b"document.title='XSSEXEC|'+document.domain;"
    b"alert('XSS on '+document.domain+' cookie='+document.cookie);"
    b"</script></body></html>"
)

# Every extension that turns a response body into a scriptable document.
ACTIVE_DOCUMENT_FILENAMES = [
    "vx.html",
    "vx.htm",
    "vx.xhtml",
    "vx.xml",
    "vx.svg",
    "vx.shtml",
    "vx.mhtml",
    "vx.xht",
]


# ---------------------------------------------------------------------------
# Policy module — the allowlist itself
# ---------------------------------------------------------------------------


class TestResourceTypePolicy:
    @pytest.mark.parametrize("filename", ACTIVE_DOCUMENT_FILENAMES)
    def test_active_document_extensions_are_refused(self, filename):
        with pytest.raises(SkillResourceTypeError):
            resolve_upload_content_type(filename)

    def test_client_supplied_content_type_is_not_a_parameter(self):
        """The signature itself is the control: there is nothing to spoof.

        The vulnerability was that the multipart header reached storage. If a
        future refactor reintroduces a caller-supplied type argument, this fails.
        """
        import inspect

        params = list(inspect.signature(resolve_upload_content_type).parameters)
        assert params == ["filename"]

    def test_allowlist_contains_no_scriptable_media_type(self):
        """No allowlist value may be a type a browser parses as a document."""
        forbidden = {
            "text/html",
            "application/xhtml+xml",
            "image/svg+xml",
            "text/xml",
            "application/xml",
            "text/javascript",
            "application/javascript",
            "application/x-shockwave-flash",
        }
        assert forbidden.isdisjoint(set(SAFE_EXTENSION_CONTENT_TYPES.values()))

    def test_code_extensions_store_as_inert_plain_text(self):
        """D5: script resources are stored, listed, and never executable."""
        for ext in ("js", "mjs", "ts", "py", "sh", "css"):
            assert resolve_upload_content_type(f"example.{ext}") == "text/plain"

    def test_ordinary_bundle_types_still_work(self):
        assert resolve_upload_content_type("forms.md") == "text/markdown"
        assert resolve_upload_content_type("data.json") == "application/json"
        assert resolve_upload_content_type("chart.png") == "image/png"
        assert resolve_upload_content_type("manual.pdf") == "application/pdf"

    def test_extension_match_is_case_insensitive(self):
        """``.HTML`` must not slip past a lowercase-only comparison."""
        with pytest.raises(SkillResourceTypeError):
            resolve_upload_content_type("vx.HTML")
        assert resolve_upload_content_type("FORMS.MD") == "text/markdown"

    def test_double_extension_resolves_on_the_last_one(self):
        """``notes.md.html`` is an HTML file, not a markdown file."""
        with pytest.raises(SkillResourceTypeError):
            resolve_upload_content_type("notes.md.html")

    def test_extensionless_filename_is_refused(self):
        with pytest.raises(SkillResourceTypeError):
            resolve_upload_content_type("payload")

    def test_refusal_message_names_the_extension(self):
        """An author who is blocked needs to know what to rename or convert."""
        with pytest.raises(SkillResourceTypeError, match=r"\.html"):
            resolve_upload_content_type("vx.html")

    def test_download_type_never_reflects_an_active_type(self):
        for filename in ACTIVE_DOCUMENT_FILENAMES:
            assert safe_download_content_type(filename) == "application/octet-stream"

    def test_download_headers_are_attachment_nosniff_and_inert_csp(self):
        headers = resource_download_headers("vx.html")
        assert headers["Content-Disposition"].startswith("attachment;")
        assert "inline" not in headers["Content-Disposition"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "sandbox" in headers["Content-Security-Policy"]

    def test_download_headers_cannot_be_broken_out_of(self):
        """A quote or CRLF in a legacy filename must not reach a header value."""
        headers = resource_download_headers('a".md\r\nX-Evil: 1')
        value = headers["Content-Disposition"]
        assert '"' not in value.removeprefix('attachment; filename="').removesuffix('"')
        assert "\r" not in value and "\n" not in value


# ---------------------------------------------------------------------------
# Write side — the unprivileged upload route
# ---------------------------------------------------------------------------


@pytest.fixture()
def author_client(user_skill_service, author_user, monkeypatch):
    """The attacker in the report: authenticated, zero admin privilege."""
    monkeypatch.setattr(
        my_skill_routes, "get_user_skill_service", lambda: user_skill_service
    )
    app = FastAPI()
    app.include_router(my_skill_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: author_user
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(skill_service, admin_user, monkeypatch):
    """The victim in the report: an ``admin.skills`` holder reading a resource."""
    monkeypatch.setattr(
        admin_skill_routes, "get_skill_catalog_service", lambda: skill_service
    )
    app = FastAPI()
    app.include_router(admin_skill_routes.router)
    override_admin_auth(app, lambda: admin_user)
    return TestClient(app)


def _create_my_skill(client, name="Verify XSS Skill QQ") -> str:
    resp = client.post(
        "/skills/mine",
        json={"displayName": name, "description": "security verification"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["skillId"]


def _upload_mine(client, skill_id, filename, body, content_type, kind="reference"):
    return client.post(
        f"/skills/mine/{skill_id}/resources",
        files={"file": (filename, body, content_type)},
        data={"kind": kind},
    )


class TestUnprivilegedUploadIsAllowlisted:
    def test_html_upload_is_rejected(self, author_client):
        """Step 4 of the report: the upload that planted the payload."""
        skill_id = _create_my_skill(author_client)
        resp = _upload_mine(
            author_client, skill_id, "vx.html", XSS_PAYLOAD, "text/html"
        )
        assert resp.status_code == 400, resp.text
        assert "not allowed" in resp.json()["detail"].lower()

        # Nothing was persisted — not in the manifest, not in S3.
        manifest = author_client.get(f"/skills/mine/{skill_id}/resources").json()
        assert manifest["resources"] == []
        s3 = boto3.client("s3", region_name=AWS_REGION)
        keys = [
            o["Key"]
            for o in s3.list_objects_v2(Bucket=SKILL_RESOURCES_BUCKET).get(
                "Contents", []
            )
        ]
        assert not any(k.endswith("vx.html") for k in keys)

    @pytest.mark.parametrize("filename", ACTIVE_DOCUMENT_FILENAMES)
    def test_every_active_document_extension_is_rejected(
        self, author_client, filename
    ):
        skill_id = _create_my_skill(author_client)
        resp = _upload_mine(
            author_client, skill_id, filename, XSS_PAYLOAD, "text/html"
        )
        assert resp.status_code == 400, f"{filename} was accepted"

    def test_html_payload_under_a_markdown_name_is_stored_as_markdown(
        self, author_client
    ):
        """Bytes are not sniffed — the *served type* is what disarms them.

        ``.md`` is allowed and HTML bytes inside a markdown file are legitimate
        (a documentation snippet), so this upload succeeds. What makes it inert
        is that it is stored and served as ``text/markdown`` with ``attachment``
        + ``nosniff``, never as a document.
        """
        skill_id = _create_my_skill(author_client)
        resp = _upload_mine(
            author_client, skill_id, "vx.md", XSS_PAYLOAD, "text/html"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["resources"][0]["contentType"] == "text/markdown"

    def test_declared_content_type_is_ignored_entirely(self, author_client):
        """The multipart header is attacker-controlled and must not be stored."""
        skill_id = _create_my_skill(author_client)
        resp = _upload_mine(
            author_client, skill_id, "notes.md", b"# notes", "text/html"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["resources"][0]["contentType"] == "text/markdown"

    def test_script_kind_does_not_bypass_the_allowlist(self, author_client):
        """``kind=script`` is about bundle layout, not about type freedom."""
        skill_id = _create_my_skill(author_client)
        resp = _upload_mine(
            author_client,
            skill_id,
            "vx.html",
            XSS_PAYLOAD,
            "text/html",
            kind="script",
        )
        assert resp.status_code == 400


class TestAdminUploadIsAllowlisted:
    """The admin catalog tier shares the code path and the same policy."""

    def _create_catalog_skill(self, admin_client, skill_id="pdf_workflows"):
        resp = admin_client.post(
            "/skills/",
            json={
                "skillId": skill_id,
                "displayName": "PDF Workflows",
                "description": "Fill, merge and split PDFs.",
                "instructions": "# PDF Workflows",
            },
        )
        assert resp.status_code == 200, resp.text
        return skill_id

    def test_html_upload_is_rejected(self, admin_client):
        skill_id = self._create_catalog_skill(admin_client)
        resp = admin_client.post(
            f"/skills/{skill_id}/resources",
            files={"file": ("vx.html", XSS_PAYLOAD, "text/html")},
        )
        assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Read side — a legacy row must be neutralized without a data migration
# ---------------------------------------------------------------------------


def _plant_legacy_html_resource(service, skill_id, filename="vx.html"):
    """Write a manifest entry the way the *vulnerable* upload path would have.

    Goes straight at the repository and the store, deliberately bypassing
    ``add_resource``, because the point is to reproduce a row that already
    exists in a deployed environment: ``content_type == "text/html"``.
    """
    from apis.shared.skills.models import SkillResourceRef
    from apis.shared.skills.resource_store import compute_content_hash

    key = service.resource_store.put(
        skill_id=skill_id,
        filename=filename,
        content=XSS_PAYLOAD,
        content_type="text/html",
        kind="reference",
    )
    ref = SkillResourceRef(
        filename=filename,
        content_hash=compute_content_hash(XSS_PAYLOAD),
        size=len(XSS_PAYLOAD),
        content_type="text/html",  # the poisoned value
        s3_key=key,
        kind="reference",
    )
    return ref


def _assert_response_cannot_execute(resp):
    """The response body may be the payload; it may not be a document."""
    assert resp.status_code == 200, resp.text
    assert resp.content == XSS_PAYLOAD  # bytes are still readable by the SPA
    assert "text/html" not in resp.headers["content-type"]
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert "inline" not in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


class TestLegacyRowIsNeutralizedOnRead:
    @pytest.mark.asyncio
    async def test_admin_read_of_a_legacy_html_row(
        self, admin_client, skill_service, admin_user
    ):
        """Step 5 of the report: the request that executed the script."""
        admin_client.post(
            "/skills/",
            json={
                "skillId": "legacy_skill",
                "displayName": "Legacy",
                "description": "d",
                "instructions": "i",
            },
        )
        ref = _plant_legacy_html_resource(skill_service, "legacy_skill")
        await skill_service.repository.update_skill(
            "legacy_skill", {"resources": [ref]}, admin_user_id=admin_user.user_id
        )

        resp = admin_client.get("/skills/legacy_skill/resources/vx.html")
        _assert_response_cannot_execute(resp)

    @pytest.mark.asyncio
    async def test_owner_read_of_a_legacy_html_row(
        self, author_client, user_skill_service, author_user
    ):
        """The owner-tier read route is hardened identically."""
        skill_id = _create_my_skill(author_client, "Legacy Mine")
        ref = _plant_legacy_html_resource(
            user_skill_service.catalog_service, skill_id
        )
        await user_skill_service.repository.update_skill(
            skill_id, {"resources": [ref]}, admin_user_id=author_user.user_id
        )

        resp = author_client.get(f"/skills/mine/{skill_id}/resources/vx.html")
        _assert_response_cannot_execute(resp)


class TestHardenedHeadersOnOrdinaryReads:
    def test_markdown_read_keeps_its_type_but_gains_the_headers(
        self, author_client
    ):
        """The SPA reads these over XHR as text, so ``attachment`` is safe."""
        skill_id = _create_my_skill(author_client, "Notes Skill")
        _upload_mine(author_client, skill_id, "forms.md", b"# Forms", "text/markdown")

        resp = author_client.get(f"/skills/mine/{skill_id}/resources/forms.md")
        assert resp.status_code == 200
        assert resp.content == b"# Forms"
        assert resp.headers["content-type"].startswith("text/markdown")
        assert resp.headers["content-disposition"].startswith("attachment;")
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_no_read_route_serves_inline(self):
        """Pins the header both tiers regressed on, at the source level."""
        import inspect

        for module in (my_skill_routes, admin_skill_routes):
            source = inspect.getsource(module)
            assert "inline; filename=" not in source, module.__name__
