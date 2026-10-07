"""Edits from the Memory tab (Shared Projects 2.8b): structured saves, deletes and the index.

Real ``MemorySpaceService`` and ``ProjectService`` over moto, as in the 2.5a-2 suite.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.memory.service import MemoryValidationError
from apis.shared.projects.memory_files import (
    ALREADY_EXISTS,
    CHANGED_SINCE,
    VIEWERS_PROPOSE,
    EditedItem,
    ProjectMemoryFiles,
    render_items_for_save,
)
from apis.shared.projects.memory_proposals import ProposalProjectError

from tests.shared.test_project_memory_items import _items, team  # noqa: F401 (fixture)
from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    STRANGER,
    VIEWER,
    env,
    gateway,
    memory,
    projects,
)


class FakeAudit:
    def __init__(self) -> None:
        self.records: list = []

    def record(self, **kw):
        self.records.append(kw)


@pytest.fixture()
def audit():
    return FakeAudit()


@pytest.fixture()
def files(projects, memory, audit):
    return ProjectMemoryFiles(repository=projects.repository, memory=memory, audit=audit)


def _index(memory, space_id) -> str:
    return memory.read_index(space_id, OWNER.user_id, OWNER.email)


class TestRender:
    def test_items_render_with_their_anchors_and_indented_continuations(self):
        text = render_items_for_save([
            EditedItem(text="Batch calls in groups of 50.", anchor="aaaaaaaa"),
            EditedItem(text="Two lines\nof one fact."),
        ])
        assert text == "- Batch calls in groups of 50. <!-- e:aaaaaaaa -->\n- Two lines\n  of one fact.\n"

    def test_no_items_is_an_empty_file(self):
        assert render_items_for_save([]) == ""


class TestSave:
    def test_a_new_file_mints_anchors_joins_the_index_and_is_audited(self, files, memory, team, audit):
        result, indexed = files.save(
            team.project_id, EDITOR, "project", "sis",
            [EditedItem(text="Term codes are YYYYTT.")], description="Banner conventions", base_version=0,
        )
        assert (result.ref.version, indexed) == (1, "added")
        assert "- [[sis]] — Banner conventions" in _index(memory, team.shared_space_id)
        [record] = audit.records
        assert record["action"] == "project.memory_edited"
        assert record["after"] == {"slug": "sis", "version": 1, "created": True}

    def test_an_edit_keeps_anchors_and_archives_what_it_leaves_out(self, files, memory, team):
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A."), EditedItem(text="B.")])
        _, (a, b), prov = _items(memory, team)
        result, indexed = files.save(
            team.project_id, OWNER, "project", "sis",
            [EditedItem(text="B, reworded.", anchor=b.anchor), EditedItem(text="C.")], base_version=1,
        )
        assert indexed is None and result.removed_anchors == [a.anchor]
        _, items, after = _items(memory, team)
        assert [i.text for i in items] == ["B, reworded.", "C."]
        assert items[0].anchor == b.anchor and after[b.anchor].added_by == EDITOR.email

    def test_a_stale_or_taken_base_version_is_a_409(self, files, team):
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A.")])
        with pytest.raises(ProposalProjectError) as taken:
            files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="B.")], base_version=0)
        assert (taken.value.status_code, str(taken.value)) == (409, ALREADY_EXISTS)
        files.save(team.project_id, OWNER, "project", "sis", [EditedItem(text="A2.")])
        with pytest.raises(ProposalProjectError) as stale:
            files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="B.")], base_version=1)
        assert (stale.value.status_code, str(stale.value)) == (409, CHANGED_SINCE)

    def test_the_pipeline_still_judges_the_text(self, files, team):
        with pytest.raises(MemoryValidationError):
            files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="See [[nowhere]].")])
        with pytest.raises(MemoryValidationError):
            files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="   ")])

    def test_a_viewer_cannot_edit_project_memory_but_edits_their_own_unaudited(self, files, projects, memory, team, audit):
        with pytest.raises(ProposalProjectError) as refused:
            files.save(team.project_id, VIEWER, "project", "sis", [EditedItem(text="A.")])
        assert (refused.value.status_code, str(refused.value)) == (403, VIEWERS_PROPOSE)
        mine = projects.get_or_create_personal_space(team.project_id, VIEWER)
        result, indexed = files.save(team.project_id, VIEWER, "mine", "prefs", [EditedItem(text="Short answers.")])
        assert (result.ref.version, indexed) == (1, "added")
        assert "[[prefs]]" in memory.read_index(mine, VIEWER.user_id, VIEWER.email)
        assert audit.records == []

    def test_an_archived_project_takes_no_edits(self, files, projects, team):
        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        with pytest.raises(ProposalProjectError) as refused:
            files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A.")])
        assert refused.value.status_code == 409


class TestDeleteAndIndex:
    def test_delete_archives_items_and_drops_only_the_files_own_index_line(self, files, memory, team, audit):
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A.")], description="Banner")
        files.save(team.project_id, EDITOR, "project", "rates", [EditedItem(text="See [[sis]].")])
        files.save_index(
            team.project_id, EDITOR, "project",
            _index(memory, team.shared_space_id) + "\nRead [[sis]] before term work.\n",
        )
        files.delete(team.project_id, EDITOR, "project", "sis")
        index = _index(memory, team.shared_space_id)
        assert "- [[sis]]" not in index and "- [[rates]]" in index
        assert "Read [[sis]] before term work." in index
        assert [r["action"] for r in audit.records][-1] == "project.memory_deleted"
        assert audit.records[-1]["before"] == {"slug": "sis"}

    def test_restoring_an_item_of_a_deleted_file_puts_the_file_back_in_the_index(self, files, memory, team):
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A.")], description="Banner")
        files.delete(team.project_id, EDITOR, "project", "sis")
        [row] = files.archive(team.project_id, EDITOR, "project")
        files.restore(team.project_id, EDITOR, "project", row.archive_id)
        assert "- [[sis]]" in _index(memory, team.shared_space_id)

    def test_the_index_save_checks_new_links(self, files, team):
        with pytest.raises(MemoryValidationError):
            files.save_index(team.project_id, EDITOR, "project", "- [[nowhere]]\n")


class TestRoutes:
    @pytest.fixture()
    def client_for(self, projects, memory, audit, monkeypatch):
        from apis.app_api.projects import memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(
            memory_routes, "_files", lambda: ProjectMemoryFiles(repository=projects.repository, memory=memory, audit=audit)
        )

        def make(user):
            app = FastAPI()
            app.include_router(memory_routes.files_router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        return make

    def test_save_delete_and_index_with_their_status_codes(self, client_for, team):
        base = f"/projects/{team.project_id}/memory"
        body = {"items": [{"text": "Term codes are YYYYTT."}], "description": "Banner", "baseVersion": 0}
        saved = client_for(EDITOR).put(f"{base}/files/people/sis", json=body).json()
        assert (saved["slug"], saved["version"], saved["indexed"], saved["itemCount"]) == ("people/sis", 1, "added", 1)

        assert client_for(EDITOR).put(f"{base}/files/people/sis", json=body).status_code == 409
        assert client_for(VIEWER).put(f"{base}/files/x", json=body).status_code == 403
        assert client_for(STRANGER).put(f"{base}/files/x", json=body).status_code == 404
        bad = client_for(EDITOR).put(f"{base}/files/x", json={"items": [{"text": "[[nowhere]]"}]})
        assert bad.status_code == 400 and "nowhere" in bad.json()["detail"]

        index = client_for(EDITOR).put(f"{base}/index", json={"content": "# Project\n- [[people/sis]]\n"})
        assert index.json() == {"content": "# Project\n- [[people/sis]]\n"}
        assert client_for(VIEWER).put(f"{base}/index", json={"content": ""}).status_code == 403

        assert client_for(VIEWER).delete(f"{base}/files/people/sis").status_code == 403
        assert client_for(EDITOR).delete(f"{base}/files/people/sis").status_code == 204
        assert client_for(EDITOR).delete(f"{base}/files/people/sis").status_code == 404

    def test_mine_needs_a_personal_space_first(self, client_for, projects, team):
        base = f"/projects/{team.project_id}/memory"
        body = {"items": [{"text": "Short answers."}]}
        assert client_for(VIEWER).put(f"{base}/files/prefs", params={"scope": "mine"}, json=body).status_code == 404
        projects.get_or_create_personal_space(team.project_id, VIEWER)
        assert client_for(VIEWER).put(f"{base}/files/prefs", params={"scope": "mine"}, json=body).status_code == 200


class TestRestoreVersion:
    def test_an_old_version_comes_back_as_a_new_one_with_its_anchors_and_provenance(self, files, memory, team, audit):
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A."), EditedItem(text="B.")], description="v1")
        _, (a, b), prov_v1 = _items(memory, team)
        files.save(
            team.project_id, OWNER, "project", "sis",
            [EditedItem(text="A.", anchor=a.anchor), EditedItem(text="C.")], description="v2",
        )
        result = files.restore_version(team.project_id, OWNER, "project", "sis", 1)
        assert result.ref.version == 3
        ref, items, prov = _items(memory, team)
        assert [(i.text, i.anchor) for i in items] == [("A.", a.anchor), ("B.", b.anchor)]
        assert ref.description == "v1"
        assert prov[b.anchor].added_by == EDITOR.email and prov[b.anchor].restored_by == OWNER.email
        archive = memory.list_archived_items(team.shared_space_id, OWNER.user_id, OWNER.email)
        assert [r.text for r in archive] == ["C."]  # B's row went; C, which v1 lacked, was archived
        assert audit.records[-1]["after"] == {"slug": "sis", "version": 3, "restoredFrom": 1}
        versions = memory.list_file_versions(team.shared_space_id, OWNER.user_id, OWNER.email, "sis")
        assert [v.reason for v in versions] == ["restore", "edit", "edit"]

    def test_the_current_version_and_pins_are_respected(self, files, memory, team):
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A.")])
        _, (a,), _ = _items(memory, team)
        files.save(team.project_id, EDITOR, "project", "sis", [EditedItem(text="A.", anchor=a.anchor), EditedItem(text="Pin me.")])
        _, (_, pinned), _ = _items(memory, team)
        memory.set_pinned(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis", pinned.anchor, pinned=True)
        with pytest.raises(MemoryValidationError):
            files.restore_version(team.project_id, EDITOR, "project", "sis", 2)
        with pytest.raises(MemoryValidationError) as blocked:
            files.restore_version(team.project_id, EDITOR, "project", "sis", 1)
        assert "pinned" in str(blocked.value).lower()

    def test_route_and_status_codes(self, files, memory, team, projects, monkeypatch):
        from apis.app_api.projects import memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(memory_routes, "_files", lambda: files)

        def client(user):
            app = FastAPI()
            app.include_router(memory_routes.files_router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        files.save(team.project_id, EDITOR, "project", "people/sis", [EditedItem(text="A.")])
        files.save(team.project_id, EDITOR, "project", "people/sis", [EditedItem(text="B.")])
        url = f"/projects/{team.project_id}/memory/history/restore"
        assert client(VIEWER).post(url, json={"slug": "people/sis", "version": 1}).status_code == 403
        assert client(EDITOR).post(url, json={"slug": "people/sis", "version": 1}).json() == {"slug": "people/sis", "version": 3}
        assert client(EDITOR).post(url, json={"slug": "people/sis", "version": 9}).status_code == 404
        assert client(EDITOR).post(url, json={"slug": "people/sis", "version": 3}).status_code == 400
