"""Item provenance, the archive and pins in project memory (Shared Projects 2.5a-2).

Real ``MemorySpaceService`` and ``ProjectService`` over moto, as in the 2.4a and
2.5a-1 suites.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.memory.format import parse_file
from apis.shared.memory.service import (
    MemoryEntryNotFoundError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemoryValidationError,
    SaveContext,
)
from apis.shared.projects.memory_files import ProjectMemoryFiles
from apis.shared.projects.memory_proposals import ProjectMemoryProposals, ProposalProjectError

from tests.shared.test_memory_spaces import TABLE as MEMORY_TABLE
from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    REGION,
    STRANGER,
    VIEWER,
    _team,
    env,
    gateway,
    memory,
    projects,
)

A = "- Batch enrollment calls in groups of 50.\n"
B = "- Term codes are YYYYTT.\n"


@pytest.fixture()
def team(projects):
    return _team(projects)


def _save(memory, team, user, text, slug="sis", **kw):
    return memory.save_entry(team.shared_space_id, user.user_id, user.email, slug, text, **kw)


def _items(memory, team, slug="sis"):
    ref, items, prov = memory.read_file_items(team.shared_space_id, OWNER.user_id, OWNER.email, slug)
    return ref, items, prov


def _line(item) -> str:
    """One item as an edit sends it back: its text, then its anchor."""
    return f"- {item.text} <!-- e:{item.anchor} -->\n"


def _anchored(memory, team, slug="sis") -> str:
    _, items, _ = _items(memory, team, slug)
    return "".join(_line(i) for i in items)


def _raw_archive(space_id):
    table = boto3.resource("dynamodb", region_name=REGION).Table(MEMORY_TABLE)
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"SPACE#{space_id}")
        & boto3.dynamodb.conditions.Key("SK").begins_with("ARCHIVE#")
    )
    return resp["Items"]


class TestProvenance:
    def test_a_new_item_records_who_when_and_the_task(self, memory, team):
        _save(memory, team, EDITOR, A, context=SaveContext(source_session_id="sess-1"))
        _, [item], prov = _items(memory, team)
        p = prov[item.anchor]
        assert (p.added_by, p.source_session_id, p.updated_by) == (EDITOR.email, "sess-1", None)
        assert p.added_at

    def test_an_edit_moves_updated_only_on_the_items_it_changed(self, memory, team):
        _save(memory, team, EDITOR, A + B)
        _, items, before = _items(memory, team)
        first, second = items
        _save(memory, team, OWNER, f"- Batch calls in groups of 25. <!-- e:{first.anchor} -->\n"
                                   + _line(second) + "- A third.\n")
        _, items, after = _items(memory, team)
        assert after[first.anchor].added_by == EDITOR.email and after[first.anchor].updated_by == OWNER.email
        assert after[second.anchor] == before[second.anchor]
        assert after[items[2].anchor].added_by == OWNER.email

    def test_an_approved_proposal_names_the_proposer_and_approver(self, memory, projects, team):
        proposals = ProjectMemoryProposals(repository=projects.repository, memory=memory, notifications=SimpleNamespace(
            notify_many=lambda *a, **k: 0, notify=lambda *a, **k: None))
        p = proposals.propose(team.project_id, VIEWER, "sis", A, source_session_id="sess-v")[0]
        proposals.approve(team.project_id, EDITOR, p.proposal_id)
        _, [item], prov = _items(memory, team)
        got = prov[item.anchor]
        assert (got.added_by, got.proposed_by, got.approved_by, got.proposal_id, got.source_session_id) == (
            EDITOR.email, VIEWER.email, EDITOR.email, p.proposal_id, "sess-v",
        )

    def test_people_are_emails_never_user_ids(self, memory, team):
        _save(memory, team, EDITOR, A)
        _, _, prov = _items(memory, team)
        assert EDITOR.user_id not in str([p.model_dump() for p in prov.values()])


class TestArchive:
    def test_an_item_a_save_leaves_out_is_archived_with_its_provenance(self, memory, team):
        _save(memory, team, EDITOR, A + B, context=SaveContext(source_session_id="sess-1"))
        _, (first, second), _ = _items(memory, team)
        _save(memory, team, OWNER, _line(first))

        [row] = memory.list_archived_items(team.shared_space_id, VIEWER.user_id, VIEWER.email)
        assert (row.slug, row.anchor, row.text, row.reason, row.archived_by) == (
            "sis", second.anchor, second.text, "removed", OWNER.email,
        )
        assert row.provenance.added_by == EDITOR.email and row.provenance.source_session_id == "sess-1"
        [raw] = _raw_archive(team.shared_space_id)
        assert abs(int(raw["ttl"]) - (time.time() + 365 * 86400)) < 120

    def test_a_personal_space_keeps_them_thirty_days(self, memory, projects, team):
        mine = projects.get_or_create_personal_space(team.project_id, VIEWER)
        memory.save_entry(mine, VIEWER.user_id, VIEWER.email, "notes", A + B)
        _, items, _ = memory.read_file_items(mine, VIEWER.user_id, VIEWER.email, "notes")
        memory.save_entry(mine, VIEWER.user_id, VIEWER.email, "notes", _line(items[0]))
        [raw] = _raw_archive(mine)
        assert abs(int(raw["ttl"]) - (time.time() + 30 * 86400)) < 120

    def test_deleting_a_file_archives_every_item(self, memory, team):
        _save(memory, team, EDITOR, A + B)
        memory.delete_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis")
        rows = memory.list_archived_items(team.shared_space_id, VIEWER.user_id, VIEWER.email)
        assert sorted(r.text for r in rows) == sorted([A[2:].strip(), B[2:].strip()])
        assert {r.reason for r in rows} == {"deleted"}
        assert memory.repository.get_provenance(team.shared_space_id, "sis") == {}

    def test_expired_rows_are_not_offered(self, memory, team):
        _save(memory, team, EDITOR, A + B)
        _, (first, _), _ = _items(memory, team)
        _save(memory, team, EDITOR, _line(first))
        [row] = memory.repository.list_archived_items(team.shared_space_id)
        memory.repository.put_archived_items(
            team.shared_space_id, [row.model_copy(update={"restorable_until": "2000-01-01T00:00:00+00:00"})], ttl=0
        )
        assert memory.list_archived_items(team.shared_space_id, EDITOR.user_id, EDITOR.email) == []


class TestRestore:
    def _archive_second(self, memory, team):
        _save(memory, team, EDITOR, A + B, context=SaveContext(source_session_id="sess-1"))
        _, (first, second), _ = _items(memory, team)
        _save(memory, team, EDITOR, _line(first))
        [row] = memory.list_archived_items(team.shared_space_id, EDITOR.user_id, EDITOR.email)
        return row, second

    def test_restoring_puts_the_item_back_with_its_anchor_and_provenance(self, memory, team):
        row, second = self._archive_second(memory, team)
        result = memory.restore_archived_item(team.shared_space_id, OWNER.user_id, OWNER.email, row.archive_id)

        _, items, prov = _items(memory, team)
        assert [i.anchor for i in items][-1] == second.anchor and items[-1].text == second.text
        p = prov[second.anchor]
        assert (p.added_by, p.source_session_id, p.restored_by) == (EDITOR.email, "sess-1", OWNER.email)
        versions = memory.repository.list_file_versions(team.shared_space_id, "sis")
        assert max(versions, key=lambda v: v.version).reason == "restore" and result.ref.version == 3
        assert memory.list_archived_items(team.shared_space_id, OWNER.user_id, OWNER.email) == []

    def test_a_deleted_file_comes_back_one_item_at_a_time(self, memory, team):
        _save(memory, team, EDITOR, A + B)
        memory.delete_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis")
        rows = memory.list_archived_items(team.shared_space_id, EDITOR.user_id, EDITOR.email)
        for row in rows:
            memory.restore_archived_item(team.shared_space_id, EDITOR.user_id, EDITOR.email, row.archive_id)
        _, items, _ = _items(memory, team)
        assert sorted(i.text for i in items) == sorted([A[2:].strip(), B[2:].strip()])

    def test_an_anchor_minted_again_since_comes_back_as_a_new_item(self, memory, team):
        row, _ = self._archive_second(memory, team)
        _, (first,), _ = _items(memory, team)
        clash = row.model_copy(update={"anchor": first.anchor})
        memory.repository.put_archived_items(team.shared_space_id, [clash], ttl=int(time.time()) + 3600)
        memory.restore_archived_item(team.shared_space_id, EDITOR.user_id, EDITOR.email, clash.archive_id)
        _, items, _ = _items(memory, team)
        assert len(items) == 2 and items[1].anchor != first.anchor

    def test_a_viewer_lists_but_cannot_restore(self, memory, team):
        row, _ = self._archive_second(memory, team)
        assert memory.list_archived_items(team.shared_space_id, VIEWER.user_id, VIEWER.email)
        with pytest.raises(MemorySpacePermissionError):
            memory.restore_archived_item(team.shared_space_id, VIEWER.user_id, VIEWER.email, row.archive_id)

    def test_an_unknown_id_is_not_found(self, memory, team):
        with pytest.raises(MemorySpaceNotFoundError):
            memory.restore_archived_item(team.shared_space_id, EDITOR.user_id, EDITOR.email, "nope")


class TestPins:
    def _pin_second(self, memory, team):
        _save(memory, team, EDITOR, A + B)
        _, (first, second), _ = _items(memory, team)
        memory.set_pinned(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis", second.anchor, pinned=True)
        return first, second

    def test_a_save_may_not_drop_a_pinned_item(self, memory, team):
        first, second = self._pin_second(memory, team)
        with pytest.raises(MemoryValidationError, match="pinned") as e:
            _save(memory, team, EDITOR, _line(first))
        assert e.value.code == "pinned_item_removed"
        # Read by a model that has no unpin tool: it must not offer to unpin it itself.
        assert "Only a person can unpin" in str(e.value)

    def test_a_save_that_keeps_it_keeps_the_pin(self, memory, team):
        first, second = self._pin_second(memory, team)
        _save(memory, team, EDITOR, _anchored(memory, team) + "- Another.\n")
        ref, _, _ = _items(memory, team)
        assert ref.pinned == [second.anchor]

    def test_unpinned_it_can_go(self, memory, team):
        first, second = self._pin_second(memory, team)
        memory.set_pinned(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis", second.anchor, pinned=False)
        _save(memory, team, EDITOR, _line(first))
        ref, items, _ = _items(memory, team)
        assert ref.pinned == [] and len(items) == 1

    def test_only_an_existing_item_by_an_editor(self, memory, team):
        _save(memory, team, EDITOR, A)
        with pytest.raises(MemoryEntryNotFoundError):
            memory.set_pinned(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis", "zzzzzzzz", pinned=True)
        _, (item,), _ = _items(memory, team)
        with pytest.raises(MemorySpacePermissionError):
            memory.set_pinned(team.shared_space_id, VIEWER.user_id, VIEWER.email, "sis", item.anchor, pinned=True)


class TestTheTool:
    def test_memory_save_records_the_task_it_ran_in(self, memory, team, monkeypatch):
        from agents.builtin_tools.memory_spaces.project_tools import ProjectMemoryScopes, make_project_memory_tools

        monkeypatch.setenv("MEMORY_SPACES_ENABLED", "true")
        monkeypatch.setattr("agents.builtin_tools.memory_spaces.project_tools.MemorySpaceService", lambda: memory)
        tools = {t.tool_spec["name"]: t for t in make_project_memory_tools(
            ProjectMemoryScopes.for_member(team.project_id, team.shared_space_id, None, EDITOR))}
        agent = SimpleNamespace(_session_manager=SimpleNamespace(config=SimpleNamespace(session_id="sess-tool")))
        result = asyncio.run(tools["memory_save"](scope="project", slug="sis", text=A, tool_context=SimpleNamespace(agent=agent)))

        assert result["status"] == "success"
        _, (item,), prov = _items(memory, team)
        assert prov[item.anchor].source_session_id == "sess-tool"

    def test_the_specs_do_not_show_the_context(self):
        from agents.builtin_tools.memory_spaces.project_tools import ProjectMemoryScopes, make_project_memory_tools
        from apis.shared.auth.models import User

        tools = make_project_memory_tools(ProjectMemoryScopes.for_member("p", "s", None, User(email="a@b", user_id="u", name="n", roles=[])))
        for t in tools:
            assert "tool_context" not in t.tool_spec["inputSchema"]["json"]["properties"]


class TestRoutes:
    @pytest.fixture()
    def client_for(self, projects, memory, monkeypatch):
        from apis.app_api.projects import memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(memory_routes, "_files", lambda: ProjectMemoryFiles(repository=projects.repository, memory=memory))
        monkeypatch.setattr(memory_routes, "display_names", lambda emails: {e: e.split("@")[0].title() for e in emails})

        def make(user):
            app = FastAPI()
            app.include_router(memory_routes.files_router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        return make

    def test_read_pin_archive_restore_and_the_status_codes(self, client_for, memory, projects, team):
        _save(memory, team, EDITOR, A + B)
        base = f"/projects/{team.project_id}/memory"
        body = client_for(VIEWER).get(f"{base}/files/sis").json()
        first, second = body["items"]
        assert first["provenance"]["addedBy"] == EDITOR.email and body["people"][EDITOR.email] == "Editor"
        assert "userId" not in str(body) and EDITOR.user_id not in str(body)

        assert client_for(VIEWER).post(f"{base}/pins", json={"slug": "sis", "anchor": second["anchor"]}).status_code == 403
        assert client_for(EDITOR).post(f"{base}/pins", json={"slug": "sis", "anchor": second["anchor"]}).json()["pinned"] == [second["anchor"]]
        assert client_for(VIEWER).get(f"{base}/files/sis").json()["items"][1]["pinned"] is True
        assert client_for(EDITOR).delete(f"{base}/pins", params={"slug": "sis", "anchor": second["anchor"]}).json()["pinned"] == []

        _save(memory, team, EDITOR, f"- {first['text']} <!-- e:{first['anchor']} -->\n")
        archive = client_for(VIEWER).get(f"{base}/archive").json()
        [row] = archive["items"]
        assert row["text"] == second["text"]
        assert archive["people"][EDITOR.email] == "Editor"
        assert client_for(VIEWER).post(f"{base}/archive/{row['archiveId']}/restore").status_code == 403
        restored = client_for(EDITOR).post(f"{base}/archive/{row['archiveId']}/restore").json()
        assert restored == {"slug": "sis", "version": 3}

        assert client_for(STRANGER).get(f"{base}/files/sis").status_code == 404
        assert client_for(VIEWER).get(f"{base}/files/sis", params={"scope": "mine"}).status_code == 404
        assert client_for(VIEWER).get(f"{base}/files/nope").status_code == 404

        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        assert client_for(EDITOR).post(f"{base}/pins", json={"slug": "sis", "anchor": first["anchor"]}).status_code == 409
