"""Voice mode in projects (#2021): the owner's switch and what keeps voice off.

``apis.shared.projects.voice`` decides; ``GET /projects/{id}/voice`` shows it to
every member and ``PATCH /projects/{id}`` lets the owner flip the switch. The voice
route's use of the same status is covered in ``test_voice_agent_plan.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.projects import voice
from apis.shared.projects.models import Project, ProjectSettings

from tests.shared.test_project_settings import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    PRINCIPALS,
    STRANGER,
    VIEWER,
    client,
    pid,
    project,
    projects_table,
    skills,
    validation,
)


@pytest.fixture()
def catalog(monkeypatch):
    """The tool catalog's voice and disabled sets, and its display names."""
    from apis.shared.tools import freshness, repository

    state = SimpleNamespace(voice={"calculator"}, disabled=set(), names={"canvas": "Canvas"})
    monkeypatch.setattr(freshness, "get_voice_tool_ids", AsyncMock(side_effect=lambda: frozenset(state.voice)))
    monkeypatch.setattr(freshness, "get_disabled_tool_ids", AsyncMock(side_effect=lambda: frozenset(state.disabled)))

    async def batch_get_tools(ids):
        return [SimpleNamespace(tool_id=i, display_name=state.names[i]) for i in ids if i in state.names]

    monkeypatch.setattr(
        repository, "get_tool_catalog_repository", lambda: SimpleNamespace(batch_get_tools=batch_get_tools)
    )
    return state


@pytest.fixture()
def no_files(monkeypatch):
    check = MagicMock(return_value=None)
    monkeypatch.setattr(voice, "files_too_large_for_voice", check)
    return check


# ── the shared status ─────────────────────────────────────────────────────


class TestToolsVoiceCannotRun:
    def test_only_tools_outside_the_voice_set_count(self, catalog):
        catalog.disabled = {"retired"}
        found = asyncio.run(voice.tools_voice_cannot_run(["calculator", "canvas", "gateway_x::lookup", "retired"]))
        # A scoped id follows its base; a disabled tool never loads, so it doesn't count.
        assert found == ["canvas", "gateway_x"]

    def test_each_tool_is_named_once(self, catalog):
        assert asyncio.run(voice.tools_voice_cannot_run(["canvas", "canvas::a", "canvas::b"])) == ["canvas"]

    def test_no_tools_reads_no_catalog(self, catalog):
        from apis.shared.tools import freshness

        assert asyncio.run(voice.tools_voice_cannot_run([])) == []
        freshness.get_voice_tool_ids.assert_not_awaited()


class TestFilesTooLargeForVoice:
    @pytest.fixture(autouse=True)
    def full_context_on(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_KB_FULL_CONTEXT_ENABLED", "true")
        monkeypatch.setenv("VOICE_KB_MAX_TOKENS", "1000")

    @pytest.mark.parametrize("reason", ["over_budget", "document_over_budget", "raw_bytes"])
    def test_size_reasons_keep_voice_off(self, reason):
        from apis.shared.assistants import kb_full_context as fc

        found = SimpleNamespace(reason=reason, estimated_tokens=4200)
        with patch.object(fc, "assess_full_context", MagicMock(return_value=found)) as assess:
            result = voice.files_too_large_for_voice("ast-h")
        assess.assert_called_once_with("ast-h", 1000)
        assert result == voice.FilesTooLarge(reason=reason, estimated_tokens=4200, budget_tokens=1000)

    @pytest.mark.parametrize("reason", [None, "empty", "no_text_documents", "no_text_layer"])
    def test_other_outcomes_do_not(self, reason):
        from apis.shared.assistants import kb_full_context as fc

        with patch.object(fc, "assess_full_context", MagicMock(return_value=SimpleNamespace(reason=reason))):
            assert voice.files_too_large_for_voice("ast-h") is None

    def test_a_deployment_that_keeps_files_out_of_voice_blocks_a_project_with_files(self, monkeypatch):
        from apis.shared.assistants import kb_documents

        monkeypatch.setenv("VOICE_KB_MAX_TOKENS", "0")
        with patch.object(kb_documents, "list_kb_documents", MagicMock(return_value=[{"status": "complete"}])):
            assert voice.files_too_large_for_voice("ast-h").reason == voice.FILES_OFF
        with patch.object(kb_documents, "list_kb_documents", MagicMock(return_value=[{"status": "processing"}])):
            assert voice.files_too_large_for_voice("ast-h") is None


def _project(voice_enabled=True) -> Project:
    return Project(
        project_id="p-1", name="P", owner_id="o", owner_email="o@example.edu", harness_agent_id="ast-h",
        settings=ProjectSettings(voice_enabled=voice_enabled),
        created_at="2026-11-01T00:00:00Z", updated_at="2026-11-01T00:00:00Z",
    )


def _harness(*tool_ids):
    return SimpleNamespace(bindings=[SimpleNamespace(kind="tool", ref=t) for t in tool_ids]
                           + [SimpleNamespace(kind="skill", ref="skill_one")])


class TestProjectVoiceStatus:
    def test_on_by_default(self, catalog, no_files):
        assert ProjectSettings().voice_enabled is True
        # A META row stored before the switch existed reads as on.
        assert ProjectSettings.model_validate({"editorsManageMembers": False}).voice_enabled is True
        status = asyncio.run(voice.project_voice_status(_project(), _harness("calculator")))
        assert (status.enabled, status.blocked, status.available) == (True, False, True)

    def test_a_blocker_keeps_voice_off_without_touching_the_switch(self, catalog, no_files):
        status = asyncio.run(voice.project_voice_status(_project(), _harness("calculator", "canvas")))
        assert status.enabled is True
        assert status.blocked_tool_ids == ("canvas",)
        assert status.available is False

    def test_the_owners_switch(self, catalog, no_files):
        status = asyncio.run(voice.project_voice_status(_project(voice_enabled=False), _harness()))
        assert (status.enabled, status.blocked, status.available) == (False, False, False)

    def test_files_can_be_skipped(self, catalog, no_files):
        asyncio.run(voice.project_voice_status(_project(), _harness(), check_files=False))
        no_files.assert_not_called()


# ── the routes ────────────────────────────────────────────────────────────


MATRIX = [
    ("GET", "/voice", None, {"owner": 200, "editor": 200, "viewer": 200, "stranger": 404}),
    ("PATCH", "", {"voiceEnabled": False}, {"owner": 200, "editor": 403, "viewer": 403, "stranger": 404}),
]


@pytest.mark.parametrize("principal", list(PRINCIPALS))
@pytest.mark.parametrize("method,suffix,body,expected", MATRIX, ids=[f"{m} {s or '/'}" for m, s, _, _ in MATRIX])
def test_authorization_matrix(pid, catalog, no_files, principal, method, suffix, body, expected):
    response = client(PRINCIPALS[principal]).request(method, f"/projects/{pid}{suffix}", json=body)
    assert response.status_code == expected[principal], response.text


def test_a_new_project_has_voice(pid, catalog, no_files):
    assert client(VIEWER).get(f"/projects/{pid}").json()["voiceEnabled"] is True
    body = client(VIEWER).get(f"/projects/{pid}/voice").json()
    assert body == {"enabled": True, "available": True, "canChange": False, "blockedTools": [], "filesTooLarge": None}
    assert client(OWNER).get(f"/projects/{pid}/voice").json()["canChange"] is True


def test_the_owner_turns_voice_off_and_on(pid, catalog, no_files):
    owner = client(OWNER)
    assert owner.patch(f"/projects/{pid}", json={"voiceEnabled": False}).json()["voiceEnabled"] is False
    assert client(EDITOR).get(f"/projects/{pid}/voice").json()["available"] is False
    owner.patch(f"/projects/{pid}", json={"voiceEnabled": True})
    assert client(EDITOR).get(f"/projects/{pid}/voice").json()["available"] is True


def test_a_tool_voice_cant_run_turns_voice_off_until_it_is_removed(pid, catalog, no_files):
    owner = client(OWNER)
    owner.put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "calculator"}, {"ref": "canvas"}, {"ref": "zoom"}]})

    body = client(EDITOR).get(f"/projects/{pid}/voice").json()
    assert (body["enabled"], body["available"]) == (True, False)
    # Named from the catalog, the id when the catalog has no name.
    assert body["blockedTools"] == [{"toolId": "canvas", "name": "Canvas"}, {"toolId": "zoom", "name": "zoom"}]

    owner.put(f"/projects/{pid}/tools", json={"bindings": [{"ref": "calculator"}]})
    assert client(EDITOR).get(f"/projects/{pid}/voice").json()["available"] is True


def test_files_too_large_turn_voice_off(pid, catalog, monkeypatch):
    found = voice.FilesTooLarge(reason="over_budget", estimated_tokens=900_000, budget_tokens=800_000)
    monkeypatch.setattr(voice, "files_too_large_for_voice", MagicMock(return_value=found))
    body = client(VIEWER).get(f"/projects/{pid}/voice").json()
    assert body["available"] is False
    assert body["filesTooLarge"] == {"reason": "over_budget", "estimatedTokens": 900_000, "budgetTokens": 800_000}


def test_a_failed_check_says_so(pid, catalog, monkeypatch):
    monkeypatch.setattr(voice, "files_too_large_for_voice", MagicMock(side_effect=RuntimeError("s3 down")))
    assert client(VIEWER).get(f"/projects/{pid}/voice").status_code == 503


def test_an_archived_project_cant_change_it(pid, project, catalog, no_files):
    service, _ = project
    asyncio.run(service.update_project(pid, OWNER, status="archived"))
    assert client(OWNER).get(f"/projects/{pid}/voice").json()["canChange"] is False
    assert client(OWNER).patch(f"/projects/{pid}", json={"voiceEnabled": False}).status_code == 409


def test_the_switch_is_audited(project, catalog):
    from apis.shared.audit import AuditAction

    service, created = project
    recorded = []
    with patch.object(service, "record", side_effect=lambda *a, **k: recorded.append((a, k))):
        asyncio.run(service.update_project(created.project_id, OWNER, voice_enabled=False))
    (args, kwargs), = recorded
    assert args[0] == AuditAction.PROJECT_UPDATED
    assert kwargs["changes"] == ["voiceEnabled"]
    assert (kwargs["before"], kwargs["after"]) == ({"voiceEnabled": True}, {"voiceEnabled": False})
