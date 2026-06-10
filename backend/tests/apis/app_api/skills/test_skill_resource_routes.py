"""Admin /skills/{id}/resources route tests (TestClient + moto S3 + DDB).

Covers the reference-file lifecycle: upload → list manifest → read bytes →
re-upload (replace) → delete, plus validation (filename, size, empty, caps),
content-hash dedupe / orphan GC, and 404s for missing skills/files.
"""

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from apis.app_api.admin.skills import routes as skill_routes
from apis.app_api.skills import service as skill_service_module

from .conftest import AWS_REGION, SKILL_RESOURCES_BUCKET


@pytest.fixture()
def client(skill_service, admin_user, monkeypatch):
    monkeypatch.setattr(
        skill_routes, "get_skill_catalog_service", lambda: skill_service
    )
    app = FastAPI()
    app.include_router(skill_routes.router)
    app.dependency_overrides[require_admin] = lambda: admin_user
    return TestClient(app)


def _create_skill(client, skill_id="pdf_workflows"):
    resp = client.post(
        "/skills/",
        json={
            "skillId": skill_id,
            "displayName": "PDF Workflows",
            "description": "Fill, merge and split PDFs.",
            "instructions": "# PDF Workflows",
            "boundToolIds": [],
        },
    )
    assert resp.status_code == 200, resp.text


def _bucket_keys():
    s3 = boto3.client("s3", region_name=AWS_REGION)
    return [
        o["Key"]
        for o in s3.list_objects_v2(Bucket=SKILL_RESOURCES_BUCKET).get("Contents", [])
    ]


def _upload(client, skill_id, filename, body, content_type="text/markdown"):
    return client.post(
        f"/skills/{skill_id}/resources",
        files={"file": (filename, body, content_type)},
    )


class TestUploadAndList:
    def test_upload_then_list_manifest(self, client):
        _create_skill(client)
        resp = _upload(client, "pdf_workflows", "forms.md", b"# Forms")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skillId"] == "pdf_workflows"
        assert len(body["resources"]) == 1
        ref = body["resources"][0]
        assert ref["filename"] == "forms.md"
        assert ref["size"] == len(b"# Forms")
        assert ref["contentType"] == "text/markdown"
        assert ref["s3Key"] == f"skills/pdf_workflows/{ref['contentHash']}"

        listed = client.get("/skills/pdf_workflows/resources")
        assert listed.status_code == 200
        assert [r["filename"] for r in listed.json()["resources"]] == ["forms.md"]

    def test_manifest_reflected_on_skill_get(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "forms.md", b"# Forms")
        skill = client.get("/skills/pdf_workflows").json()
        assert [r["filename"] for r in skill["resources"]] == ["forms.md"]

    def test_multiple_files_sorted(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "zebra.md", b"z")
        _upload(client, "pdf_workflows", "alpha.md", b"a")
        names = [
            r["filename"]
            for r in client.get("/skills/pdf_workflows/resources").json()["resources"]
        ]
        assert names == ["alpha.md", "zebra.md"]


class TestReadBytes:
    def test_read_returns_bytes_and_content_type(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "forms.md", b"# Forms body")
        resp = client.get("/skills/pdf_workflows/resources/forms.md")
        assert resp.status_code == 200
        assert resp.content == b"# Forms body"
        assert resp.headers["content-type"].startswith("text/markdown")

    def test_read_missing_file_404(self, client):
        _create_skill(client)
        assert (
            client.get("/skills/pdf_workflows/resources/nope.md").status_code == 404
        )


class TestReplaceAndDedupe:
    def test_reupload_same_filename_replaces(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "forms.md", b"v1")
        resp = _upload(client, "pdf_workflows", "forms.md", b"v2-longer")
        assert resp.status_code == 200
        manifest = resp.json()["resources"]
        assert len(manifest) == 1  # replaced, not appended
        assert manifest[0]["size"] == len(b"v2-longer")
        # Reading back returns the new content.
        assert (
            client.get("/skills/pdf_workflows/resources/forms.md").content
            == b"v2-longer"
        )

    def test_reupload_garbage_collects_orphaned_object(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "forms.md", b"v1")
        _upload(client, "pdf_workflows", "forms.md", b"v2")
        # Only the current object remains; the v1 object was GC'd.
        keys = _bucket_keys()
        assert len(keys) == 1

    def test_identical_content_two_filenames_dedupes_object(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "a.md", b"identical")
        _upload(client, "pdf_workflows", "b.md", b"identical")
        # Manifest has both filenames...
        names = [
            r["filename"]
            for r in client.get("/skills/pdf_workflows/resources").json()["resources"]
        ]
        assert names == ["a.md", "b.md"]
        # ...but only one S3 object (content-addressed).
        assert len(_bucket_keys()) == 1

    def test_delete_one_keeps_shared_object(self, client):
        # a.md and b.md share one content-addressed object; deleting a.md must
        # NOT delete the object b.md still references.
        _create_skill(client)
        _upload(client, "pdf_workflows", "a.md", b"identical")
        _upload(client, "pdf_workflows", "b.md", b"identical")
        client.delete("/skills/pdf_workflows/resources/a.md")
        assert len(_bucket_keys()) == 1
        assert client.get("/skills/pdf_workflows/resources/b.md").content == b"identical"


class TestDelete:
    def test_delete_removes_from_manifest_and_object(self, client):
        _create_skill(client)
        _upload(client, "pdf_workflows", "forms.md", b"x")
        resp = client.delete("/skills/pdf_workflows/resources/forms.md")
        assert resp.status_code == 200
        assert resp.json()["resources"] == []
        assert _bucket_keys() == []
        assert (
            client.get("/skills/pdf_workflows/resources/forms.md").status_code == 404
        )

    def test_delete_missing_file_404(self, client):
        _create_skill(client)
        assert (
            client.delete("/skills/pdf_workflows/resources/nope.md").status_code
            == 404
        )


class TestValidationAnd404s:
    def test_upload_missing_skill_404(self, client):
        resp = _upload(client, "ghost_skill", "forms.md", b"x")
        assert resp.status_code == 404

    def test_list_missing_skill_404(self, client):
        assert client.get("/skills/ghost_skill/resources").status_code == 404

    def test_invalid_filename_400(self, client):
        _create_skill(client)
        resp = _upload(client, "pdf_workflows", "../etc/passwd", b"x")
        assert resp.status_code == 400
        assert "Invalid reference filename" in resp.json()["detail"]

    def test_empty_file_400(self, client):
        _create_skill(client)
        resp = _upload(client, "pdf_workflows", "forms.md", b"")
        assert resp.status_code == 400

    def test_too_large_400(self, client, monkeypatch):
        _create_skill(client)
        monkeypatch.setattr(skill_service_module, "MAX_RESOURCE_BYTES", 8)
        resp = _upload(client, "pdf_workflows", "forms.md", b"way too many bytes")
        assert resp.status_code == 400

    def test_count_cap_400(self, client, monkeypatch):
        _create_skill(client)
        monkeypatch.setattr(skill_service_module, "MAX_RESOURCES_PER_SKILL", 1)
        assert _upload(client, "pdf_workflows", "a.md", b"a").status_code == 200
        resp = _upload(client, "pdf_workflows", "b.md", b"b")
        assert resp.status_code == 400
        assert "maximum" in resp.json()["detail"]

    def test_count_cap_allows_replacing_existing(self, client, monkeypatch):
        _create_skill(client)
        monkeypatch.setattr(skill_service_module, "MAX_RESOURCES_PER_SKILL", 1)
        assert _upload(client, "pdf_workflows", "a.md", b"a").status_code == 200
        # Replacing the same filename is allowed even at the cap.
        assert _upload(client, "pdf_workflows", "a.md", b"a2").status_code == 200
