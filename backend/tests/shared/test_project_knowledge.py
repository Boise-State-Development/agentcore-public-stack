"""A project's Files over its harness's documents (shared-projects PR-1.5b, §9.5).

Reads are open to every member (the agent document routes are editor-only);
writes need an editor on an active project and run the agent's own handlers,
which is where upload provisioning, the byte cap and cleanup live.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.app_api.documents.routes as document_routes
import apis.app_api.projects.knowledge_routes as knowledge_routes
import apis.app_api.projects.routes as project_routes
from apis.app_api.documents.models import DocumentProvenance
from apis.app_api.documents.services.document_service import create_document
from apis.app_api.web_sources.models import ActiveCrawlsResponse
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

from tests.shared.test_project_harness_rules import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    STRANGER,
    VIEWER,
    project,
    projects_table,
)


@pytest.fixture()
def pid(project, monkeypatch) -> str:
    service, created = project
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    monkeypatch.setattr(project_routes, "_service", service)
    # Everything the upload handler does outside the documents table.
    monkeypatch.setattr(document_routes, "begin_born_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(document_routes, "_reserve_managed_upload", AsyncMock(return_value=0))
    monkeypatch.setattr(document_routes, "generate_upload_url", AsyncMock(return_value=("https://s3/put", "k")))
    monkeypatch.setattr(document_routes, "_resolve_kb_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(knowledge_routes, "generate_download_url", AsyncMock(return_value="https://s3/get"))
    return created.project_id


@pytest.fixture()
def harness_id(project) -> str:
    return project[1].harness_agent_id


def client(user: User) -> TestClient:
    app = FastAPI()
    app.include_router(knowledge_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _doc(harness_id: str, name: str, **kw) -> str:
    return asyncio.run(create_document(
        assistant_id=harness_id, filename=name, content_type="text/plain", size_bytes=3,
        s3_key=f"k/{name}", **kw,
    )).document_id


def _upload(user: User, pid: str, name: str = "notes.txt"):
    return client(user).post(
        f"/projects/{pid}/knowledge/upload-url",
        json={"filename": name, "contentType": "text/plain", "sizeBytes": 3},
    )


def test_members_read_files_and_editors_add_them(pid, harness_id, monkeypatch):
    doc = _doc(harness_id, "seed.txt", added_by_user_id=OWNER.user_id)
    base = f"/projects/{pid}/knowledge"
    monkeypatch.setattr("apis.shared.sync_policies.service.delete_sync_policies_for_source", AsyncMock())
    monkeypatch.setattr("apis.app_api.documents.services.cleanup_service.cleanup_document_resources", AsyncMock())

    expected = {
        ("GET", ""): {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404},
        ("GET", f"/{doc}"): {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404},
        ("GET", f"/{doc}/download"): {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404},
        ("GET", f"/{doc}/chunks"): {"viewer": 403, "stranger": 404},
        ("POST", "/upload-url"): {"owner": 200, "editor": 200, "viewer": 403, "stranger": 404},
        ("DELETE", f"/{doc}"): {"viewer": 403, "stranger": 404, "editor": 204},
    }
    users = {"owner": OWNER, "editor": EDITOR, "viewer": VIEWER, "stranger": STRANGER}
    body = {"filename": "a.txt", "contentType": "text/plain", "sizeBytes": 3}
    for (method, suffix), by_role in expected.items():
        for role, code in by_role.items():
            response = client(users[role]).request(method, base + suffix, json=body if method == "POST" else None)
            assert response.status_code == code, (method, suffix, role, response.text)


def test_files_say_who_added_them(pid, harness_id, project):
    service, _ = project
    service.get_project(pid, EDITOR)  # back-fill the editor's userId, as their first visit does
    assert _upload(EDITOR, pid).status_code == 200
    _doc(harness_id, "drive.txt", provenance=DocumentProvenance(
        source_connector_id="gdrive", source_adapter_key="google", source_file_id="f1",
        imported_by_user_id=OWNER.user_id,
    ))
    _doc(harness_id, "legacy.txt")                                    # before addedBy existed
    _doc(harness_id, "gone.txt", added_by_user_id="u-former-member")  # left the project

    files = client(VIEWER).get(f"/projects/{pid}/knowledge").json()
    by_name = {f["filename"]: f["addedByEmail"] for f in files["documents"]}
    assert by_name == {
        "notes.txt": EDITOR.email, "drive.txt": OWNER.email, "legacy.txt": None, "gone.txt": None,
    }
    assert files["canEdit"] is False
    assert not any("addedByUserId" in f or "importedByUserId" in f for f in files["documents"])


def test_upload_carries_the_sharing_notice(pid):
    body = _upload(EDITOR, pid).json()
    assert body["uploadUrl"] == "https://s3/put"
    assert body["notice"].startswith("Everyone in Enrollment Sync (3 people) can open this file")


def test_an_archived_projects_files_are_read_only(pid, harness_id, project):
    service, _ = project
    doc = _doc(harness_id, "kept.txt")
    asyncio.run(service.update_project(pid, OWNER, status="archived"))

    assert _upload(OWNER, pid).status_code == 409
    assert client(OWNER).delete(f"/projects/{pid}/knowledge/{doc}").status_code == 409
    listing = client(VIEWER).get(f"/projects/{pid}/knowledge").json()
    assert ([d["filename"] for d in listing["documents"]], listing["canEdit"]) == (["kept.txt"], False)
    # And the agent routes cannot be used to get around it (the harness caps members at viewer).
    agent_app = FastAPI()
    agent_app.include_router(document_routes.router)
    agent_app.dependency_overrides[get_current_user_from_session] = lambda: OWNER
    response = TestClient(agent_app, raise_server_exceptions=False).delete(
        f"/assistants/{harness_id}/documents/{doc}"
    )
    assert response.status_code == 403


def test_crawl_routes_are_not_read_as_document_ids(pid, harness_id, monkeypatch):
    listed = AsyncMock(return_value=ActiveCrawlsResponse(crawls=[]))
    monkeypatch.setattr(knowledge_routes.crawl_routes, "list_crawls", listed)
    assert client(EDITOR).get(f"/projects/{pid}/knowledge/crawls?active=true").status_code == 200
    assert listed.await_args.args[:2] == (harness_id, True)
    assert client(VIEWER).get(f"/projects/{pid}/knowledge/crawls").status_code == 403


def test_a_missing_file_is_404(pid):
    assert client(VIEWER).get(f"/projects/{pid}/knowledge/DOC-nope").status_code == 404
    assert client(VIEWER).get(f"/projects/{pid}/knowledge/DOC-nope/download").status_code == 404


def test_adding_and_removing_files_is_on_the_projects_audit_trail(pid, harness_id, project, monkeypatch):
    from tests.shared.test_project_settings import AuditRecorder

    service, _ = project
    trail = AuditRecorder()
    monkeypatch.setattr(service, "audit", trail)
    monkeypatch.setattr("apis.shared.sync_policies.service.delete_sync_policies_for_source", AsyncMock())
    monkeypatch.setattr("apis.app_api.documents.services.cleanup_service.cleanup_document_resources", AsyncMock())

    doc_id = _upload(EDITOR, pid, "plan.txt").json()["documentId"]
    assert client(EDITOR).delete(f"/projects/{pid}/knowledge/{doc_id}").status_code == 204

    assert [(r["action"], r["actor"].user_id) for r in trail.records] == [
        ("project.knowledge_added", EDITOR.user_id),
        ("project.knowledge_removed", EDITOR.user_id),
    ]
    assert trail.records[0]["after"] == {"documentId": doc_id, "filename": "plan.txt", "source": "upload"}
    assert trail.records[1]["before"] == {"documentId": doc_id, "filename": "plan.txt"}
