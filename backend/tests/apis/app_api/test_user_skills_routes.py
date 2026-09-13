"""Route tests for the user-facing skills API.

Covers the picker (GET /skills/, PUT /skills/preferences) and the
read-only detail surface behind Customize → Skills → one skill
(GET /skills/{id}, GET /skills/{id}/resources/{filename}).
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.skills.models import (
    SkillDefinition,
    SkillResourceRef,
    SkillStatus,
    UserSkillPreference,
)

from apis.app_api.skills import routes as skills_routes


def _user() -> User:
    return User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        roles=["default"],
        raw_token="tok",
    )


def _skill(skill_id: str, display_name: str, status=SkillStatus.ACTIVE):
    return SkillDefinition(
        skill_id=skill_id,
        display_name=display_name,
        description=f"{display_name} description",
        instructions="...",
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
                _skill("web_research", "Web Research"),
                _skill("pdf_workflows", "PDF Workflows"),
            ],
            prefs={"web_research": True},
        )
        client = _make_client(monkeypatch, ["web_research", "pdf_workflows"], repo)

        body = client.get("/skills/").json()
        assert body["totalCount"] == 2
        by_id = {s["skillId"]: s for s in body["skills"]}

        # Toggled-on skill: explicit preference surfaces, effective on
        assert by_id["web_research"]["userEnabled"] is True
        assert by_id["web_research"]["isEnabled"] is True

        # Untouched skill: no preference, DISABLED by default. Skills v2 D6
        # flips this from v1 — the picker is opt-in, unlike tools, and this must
        # agree with the runtime's absent-selection-means-no-skills default or
        # the UI would show skills as active that the turn never loads.
        assert by_id["pdf_workflows"]["userEnabled"] is None
        assert by_id["pdf_workflows"]["isEnabled"] is False

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


class _FakeCatalogService:
    """Duck-typed stand-in for SkillCatalogService (detail + resource read)."""

    def __init__(self, skills: List[SkillDefinition], blobs: Dict[str, bytes] = None):
        self._skills = {s.skill_id: s for s in skills}
        self._blobs = dict(blobs or {})

    async def get_skill(self, skill_id: str):
        return self._skills.get(skill_id)

    async def read_resource(self, skill_id: str, filename: str):
        skill = self._skills.get(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")
        ref = next((r for r in skill.resources if r.filename == filename), None)
        if ref is None:
            raise ValueError(f"Reference file '{filename}' not found")
        return ref, self._blobs.get(filename, b"")


def _make_detail_client(
    monkeypatch: pytest.MonkeyPatch,
    accessible: List[str],
    repo: _FakeRepo,
    catalog: _FakeCatalogService,
) -> TestClient:
    client = _make_client(monkeypatch, accessible, repo)
    monkeypatch.setattr(skills_routes, "get_skill_catalog_service", lambda: catalog)
    return client


class TestGetAccessibleSkill:
    def test_returns_the_instructions_body_and_merged_preference(self, monkeypatch):
        skill = _skill("web_research", "Web Research")
        skill.instructions = "# Web Research\n\nSearch broadly, cite everything."
        skill.category = "research"
        skill.allowed_tools = ["web_search"]
        repo = _FakeRepo(skills=[skill], prefs={"web_research": True})
        client = _make_detail_client(
            monkeypatch, ["web_research"], repo, _FakeCatalogService([skill])
        )

        body = client.get("/skills/web_research").json()
        assert body["skillId"] == "web_research"
        # The point of the endpoint: GET /skills/ carries none of this.
        assert body["instructions"] == "# Web Research\n\nSearch broadly, cite everything."
        assert body["allowedTools"] == ["web_search"]
        assert body["category"] == "research"
        assert body["userEnabled"] is True
        assert body["isEnabled"] is True

    def test_untouched_skill_reads_off_like_the_picker(self, monkeypatch):
        """D6 opt-in. The detail page must not contradict the list it came from."""
        skill = _skill("pdf_workflows", "PDF Workflows")
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["pdf_workflows"], repo, _FakeCatalogService([skill])
        )

        body = client.get("/skills/pdf_workflows").json()
        assert body["userEnabled"] is None
        assert body["isEnabled"] is False

    def test_inaccessible_skill_is_404_not_403(self, monkeypatch):
        """A skill you weren't granted must be indistinguishable from one that
        does not exist — a 403 would confirm it is out there."""
        skill = _skill("someone_elses", "Someone Else's")
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["web_research"], repo, _FakeCatalogService([skill])
        )

        assert client.get("/skills/someone_elses").status_code == 404

    def test_non_active_catalog_skill_is_404(self, monkeypatch):
        """GET /skills/ filters to ACTIVE, so a drilled-in DRAFT must not be
        reachable by id from a surface that never listed it."""
        skill = _skill("draft_one", "Draft One", status=SkillStatus.DRAFT)
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["draft_one"], repo, _FakeCatalogService([skill])
        )

        assert client.get("/skills/draft_one").status_code == 404

    def test_owner_still_reads_their_own_non_active_skill(self, monkeypatch):
        """Ownership is its own grant: a user's own draft is theirs to open."""
        skill = _skill("my_draft", "My Draft", status=SkillStatus.DRAFT)
        skill.owner_id = "user-1"
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["my_draft"], repo, _FakeCatalogService([skill])
        )

        body = client.get("/skills/my_draft").json()
        assert body["isOwned"] is True
        assert body["status"] == "draft"

    def test_catalog_skill_is_not_owned(self, monkeypatch):
        skill = _skill("web_research", "Web Research")  # owner_id defaults to "system"
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["web_research"], repo, _FakeCatalogService([skill])
        )

        assert client.get("/skills/web_research").json()["isOwned"] is False

    def test_does_not_leak_owner_id_or_role_topology(self, monkeypatch):
        """`ownerId` would name another user; `allowedAppRoles` is an
        admin-display projection of RBAC (CLAUDE.md) and has no business on a
        surface any granted user can open."""
        skill = _skill("web_research", "Web Research")
        skill.owner_id = "someone-else"
        skill.allowed_app_roles = ["faculty", "staff"]
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["web_research"], repo, _FakeCatalogService([skill])
        )

        body = client.get("/skills/web_research").json()
        assert "ownerId" not in body
        assert "allowedAppRoles" not in body

    def test_mine_still_routes_to_the_list_not_a_skill_called_mine(self, monkeypatch):
        """⚠️ Registration-order guard. SKILL_ID_PATTERN matches the literal
        'mine', so declaring /{skill_id} above the /mine routes would turn
        GET /skills/mine into a lookup for a skill named "mine"."""
        repo = _FakeRepo(skills=[], prefs={})
        client = _make_detail_client(monkeypatch, [], repo, _FakeCatalogService([]))

        async def fake_list(user):
            return []

        monkeypatch.setattr(
            skills_routes.get_user_skill_service(), "list_my_skills", fake_list
        )
        resp = client.get("/skills/mine")
        assert resp.status_code == 200
        assert resp.json() == {"skills": [], "totalCount": 0}


class TestReadAccessibleSkillResource:
    def _skill_with_file(self) -> SkillDefinition:
        skill = _skill("web_research", "Web Research")
        skill.resources = [
            SkillResourceRef(
                filename="forms.md",
                content_hash="abc123",
                size=11,
                content_type="text/markdown",
                s3_key="skills/web_research/references/forms.md",
            )
        ]
        return skill

    def test_serves_a_granted_skills_file_the_user_does_not_own(self, monkeypatch):
        skill = self._skill_with_file()
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch,
            ["web_research"],
            repo,
            _FakeCatalogService([skill], {"forms.md": b"hello world"}),
        )

        resp = client.get("/skills/web_research/resources/forms.md")
        assert resp.status_code == 200
        assert resp.content == b"hello world"
        # Served inert: never a script-bearing document on the SPA's origin.
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in resp.headers["content-disposition"]

    def test_inaccessible_skills_file_is_404(self, monkeypatch):
        skill = self._skill_with_file()
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, [], repo, _FakeCatalogService([skill], {"forms.md": b"x"})
        )

        assert client.get("/skills/web_research/resources/forms.md").status_code == 404

    def test_unknown_filename_is_404(self, monkeypatch):
        skill = self._skill_with_file()
        repo = _FakeRepo(skills=[skill], prefs={})
        client = _make_detail_client(
            monkeypatch, ["web_research"], repo, _FakeCatalogService([skill])
        )

        assert client.get("/skills/web_research/resources/nope.md").status_code == 404
