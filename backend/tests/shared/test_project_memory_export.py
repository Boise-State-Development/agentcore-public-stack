"""A project's memory as a zip, with provenance.json (Shared Projects 2.7, §5).

Real ``MemorySpaceService`` and ``ProjectService`` over moto, as in the 2.5a-2 suite.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.projects.memory_files import EditedItem, ProjectMemoryFiles

from tests.shared.test_project_memory_items import _items, team  # noqa: F401 (fixture)
from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    STRANGER,
    VIEWER,
    env,
    gateway,
    memory,
    projects,
)


@pytest.fixture()
def files(projects, memory):
    return ProjectMemoryFiles(repository=projects.repository, memory=memory)


def _save(files, team, *texts, user=EDITOR, scope="project", slug="sis", **kw):
    return files.save(team.project_id, user, scope, slug, [EditedItem(text=t) for t in texts], **kw)[0]


class TestExport:
    @pytest.fixture()
    def client_for(self, projects, files, monkeypatch):
        from apis.app_api.projects import memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(memory_routes, "_files", lambda: files)

        def make(user):
            app = FastAPI()
            app.include_router(memory_routes.files_router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        return make

    def test_the_export_carries_provenance_json(self, client_for, files, memory, team):
        _save(files, team, "A fact.", "Another.", description="Banner")
        _, (first, second), _ = _items(memory, team)
        files.set_pinned(team.project_id, EDITOR, "project", "sis", first.anchor, pinned=True)
        response = client_for(VIEWER).get(f"/projects/{team.project_id}/memory/export")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = archive.namelist()
        root = names[0].split("/")[0]
        assert {f"{root}/MEMORY.md", f"{root}/entries/fact/sis.md", f"{root}/metadata.json", f"{root}/provenance.json"} <= set(names)
        provenance = json.loads(archive.read(f"{root}/provenance.json"))
        assert provenance["format"] == "memory-provenance/1"
        assert (provenance["scope"], provenance["projectId"]) == ("shared", team.project_id)
        [entry] = provenance["files"]
        assert (entry["slug"], entry["path"], entry["version"], entry["pinned"]) == ("sis", "entries/fact/sis.md", 1, [first.anchor])
        assert [i["anchor"] for i in entry["items"]] == [first.anchor, second.anchor]
        assert entry["items"][0]["addedBy"] == EDITOR.email and "addedAt" in entry["items"][0]
        assert "userId" not in json.dumps(entry)

    def test_mine_exports_only_the_callers_own_and_strangers_get_nothing(self, client_for, projects, files, team):
        base = f"/projects/{team.project_id}/memory/export"
        assert client_for(VIEWER).get(base, params={"scope": "mine"}).status_code == 404
        projects.get_or_create_personal_space(team.project_id, VIEWER)
        _save(files, team, "Short answers.", user=VIEWER, scope="mine", slug="prefs")
        mine = zipfile.ZipFile(io.BytesIO(client_for(VIEWER).get(base, params={"scope": "mine"}).content))
        assert any(n.endswith("entries/fact/prefs.md") for n in mine.namelist())
        assert client_for(STRANGER).get(base).status_code == 404
