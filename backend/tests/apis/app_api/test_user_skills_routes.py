"""Route tests for the user-facing skills API (GET /skills/, PUT /skills/preferences)."""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.skills.models import SkillDefinition, SkillStatus, UserSkillPreference

from apis.app_api.skills import routes as skills_routes


def _user() -> User:
    return User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        roles=["default"],
        raw_token="tok",
    )


def _skill(skill_id: str, display_name: str, status=SkillStatus.ACTIVE, bound=None):
    return SkillDefinition(
        skill_id=skill_id,
        display_name=display_name,
        description=f"{display_name} description",
        instructions="...",
        bound_tool_ids=bound or [],
        status=status,
    )


class _FakeRepo:
    """Duck-typed stand-in for SkillCatalogRepository."""

    def __init__(self, skills: List[SkillDefinition], prefs: Dict[str, bool]):
        self._skills = {s.skill_id: s for s in skills}
        self.prefs = dict(prefs)
        self.saved: Optional[Dict[str, bool]] = None

    async def batch_get_skills(self, skill_ids):
        return [self._skills[sid] for sid in skill_ids if sid in self._skills]

    async def get_user_preferences(self, user_id):
        return UserSkillPreference(user_id=user_id, skill_preferences=self.prefs)

    async def save_user_preferences(self, user_id, preferences):
        self.saved = dict(preferences)
        self.prefs.update(preferences)
        return UserSkillPreference(user_id=user_id, skill_preferences=self.prefs)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    accessible: List[str],
    repo: _FakeRepo,
) -> TestClient:
    async def fake_resolve(user):
        return list(accessible)

    monkeypatch.setattr(skills_routes, "resolve_accessible_skill_ids", fake_resolve)
    monkeypatch.setattr(skills_routes, "get_skill_catalog_repository", lambda: repo)

    app = FastAPI()
    app.include_router(skills_routes.router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return TestClient(app)


class TestGetUserSkills:
    def test_lists_active_accessible_skills_with_prefs_merged(self, monkeypatch):
        repo = _FakeRepo(
            skills=[
                _skill("web_research", "Web Research", bound=["t1", "t2"]),
                _skill("pdf_workflows", "PDF Workflows"),
            ],
            prefs={"web_research": False},
        )
        client = _make_client(monkeypatch, ["web_research", "pdf_workflows"], repo)

        body = client.get("/skills/").json()
        assert body["totalCount"] == 2
        by_id = {s["skillId"]: s for s in body["skills"]}

        # Toggled-off skill: explicit preference surfaces, effective off
        assert by_id["web_research"]["userEnabled"] is False
        assert by_id["web_research"]["isEnabled"] is False
        assert by_id["web_research"]["boundToolCount"] == 2

        # Untouched skill: no preference, enabled by default
        assert by_id["pdf_workflows"]["userEnabled"] is None
        assert by_id["pdf_workflows"]["isEnabled"] is True

        # Sorted by display name
        assert [s["skillId"] for s in body["skills"]] == [
            "pdf_workflows",
            "web_research",
        ]

    def test_non_active_skills_are_hidden(self, monkeypatch):
        repo = _FakeRepo(
            skills=[
                _skill("active_one", "Active One"),
                _skill("draft_one", "Draft One", status=SkillStatus.DRAFT),
                _skill("disabled_one", "Disabled One", status=SkillStatus.DISABLED),
            ],
            prefs={},
        )
        client = _make_client(
            monkeypatch, ["active_one", "draft_one", "disabled_one"], repo
        )

        body = client.get("/skills/").json()
        assert [s["skillId"] for s in body["skills"]] == ["active_one"]

    def test_no_accessible_skills_returns_empty(self, monkeypatch):
        repo = _FakeRepo(skills=[], prefs={})
        client = _make_client(monkeypatch, [], repo)

        body = client.get("/skills/").json()
        assert body == {"skills": [], "totalCount": 0}


class TestUpdateSkillPreferences:
    def test_saves_preferences_for_accessible_skills(self, monkeypatch):
        repo = _FakeRepo(skills=[_skill("web_research", "Web Research")], prefs={})
        client = _make_client(monkeypatch, ["web_research"], repo)

        resp = client.put(
            "/skills/preferences",
            json={"preferences": {"web_research": False}},
        )
        assert resp.status_code == 200
        assert repo.saved == {"web_research": False}

    def test_rejects_inaccessible_skill_ids(self, monkeypatch):
        repo = _FakeRepo(skills=[_skill("web_research", "Web Research")], prefs={})
        client = _make_client(monkeypatch, ["web_research"], repo)

        resp = client.put(
            "/skills/preferences",
            json={"preferences": {"web_research": True, "forbidden_skill": True}},
        )
        assert resp.status_code == 400
        assert "forbidden_skill" in resp.json()["detail"]
        assert repo.saved is None
