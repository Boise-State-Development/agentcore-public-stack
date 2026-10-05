"""The share dialog's skill disclosure — ``GET /agents/{id}/shares/exposed-skills`` (§6/D7).

Invoke-through lets anyone an Agent is shared with use, and read, the skills its owner
wrote and bound. The marketplace submit dialog already enumerates those; this route gives
the share dialog the same list, from the same helper (``listing_service.exposed_skills``),
so the two disclosures and the turn path cannot disagree about what widening an Agent
gives away.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agent_designer.routes import router
from apis.shared.assistants.models import AgentBinding, AgentListing, AgentVersion, Assistant
from tests.routes.conftest import mock_auth_user

ROUTES_MODULE = "apis.app_api.agent_designer.routes"
LISTING_MODULE = "apis.app_api.agent_designer.services.listing_service"
RESOLUTION_MODULE = "apis.shared.assistants.version_resolution"
URL = "/agents/ast-001/shares/exposed-skills"


def _make_assistant(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-001",
        ownerName="Test User",
        name="Policy Lookup",
        description="Find and cite university policy",
        instructions="Answer from the policy manual.",
        vectorIndexId="idx-001",
        visibility="SHARED",
        usageCount=0,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


def _skill_bindings(*refs: str) -> list:
    return [AgentBinding(kind="skill", ref=ref, config={}) for ref in refs]


# The catalog as the repository would return it. ``skill-b`` is a skill the owner can
# reach but did not write — invoke-through does not carry it, so it is not theirs to list.
CATALOG = {
    "skill-a": SimpleNamespace(skill_id="skill-a", display_name="Policy Citation Format", owner_id="user-001"),
    "skill-b": SimpleNamespace(skill_id="skill-b", display_name="Someone Else's Skill", owner_id="user-999"),
    "skill-c": SimpleNamespace(skill_id="skill-c", display_name="Draft-Only Skill", owner_id="user-001"),
}


async def _batch_get(refs):
    return [CATALOG[r] for r in refs if r in CATALOG]


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")
    # Sharing does not need the marketplace, so neither does its disclosure.
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")


@pytest.fixture
def catalog():
    with patch(f"{LISTING_MODULE}.skills_enabled", return_value=True), patch(
        f"{LISTING_MODULE}.get_skill_catalog_repository"
    ) as repo:
        repo.return_value.batch_get_skills = AsyncMock(side_effect=_batch_get)
        yield repo.return_value


def _as(assistant, permission: str = "owner"):
    return patch(
        f"{ROUTES_MODULE}.resolve_assistant_permission",
        new_callable=AsyncMock,
        return_value=(assistant, permission),
    )


class TestShareExposure:
    def test_names_only_the_skills_the_owner_wrote(self, app, make_user, catalog):
        """Same rule as invoke-through: ``skill.owner_id == agent.owner_id``, nothing wider."""
        mock_auth_user(app, make_user())
        with _as(_make_assistant(bindings=_skill_bindings("skill-a", "skill-b"))):
            resp = TestClient(app).get(URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["agentId"] == "ast-001"
        assert body["exposedSkills"] == [{"ref": "skill-a", "label": "Policy Citation Format"}]

    def test_no_skill_bindings_discloses_nothing(self, app, make_user, catalog):
        mock_auth_user(app, make_user())
        with _as(_make_assistant(bindings=[])):
            resp = TestClient(app).get(URL)

        assert resp.status_code == 200
        assert resp.json()["exposedSkills"] == []
        catalog.batch_get_skills.assert_not_called()

    def test_skills_off_discloses_nothing(self, app, make_user):
        """With Skills killed nothing resolves on the turn path, so nothing is exposed."""
        mock_auth_user(app, make_user())
        with _as(_make_assistant(bindings=_skill_bindings("skill-a"))), patch(
            f"{LISTING_MODULE}.skills_enabled", return_value=False
        ):
            resp = TestClient(app).get(URL)

        assert resp.json()["exposedSkills"] == []

    def test_an_unreadable_catalog_still_discloses_by_ref(self, app, make_user):
        """A lookup failure errs toward warning, never toward a silent empty list."""
        mock_auth_user(app, make_user())
        with _as(_make_assistant(bindings=_skill_bindings("skill-a"))), patch(
            f"{LISTING_MODULE}.skills_enabled", return_value=True
        ), patch(f"{LISTING_MODULE}.get_skill_catalog_repository") as repo:
            repo.return_value.batch_get_skills = AsyncMock(side_effect=RuntimeError("boom"))
            resp = TestClient(app).get(URL)

        assert resp.status_code == 200
        assert resp.json()["exposedSkills"] == [{"ref": "skill-a", "label": "skill-a"}]


class TestWhatRecipientsRun:
    """A recipient of a published Agent runs the published snapshot, not the owner's draft."""

    def _published(self, *draft_refs: str) -> Assistant:
        return _make_assistant(
            visibility="PUBLIC",
            bindings=_skill_bindings(*draft_refs),
            listing=AgentListing(
                state="published", category="Teaching", publisherId="user-user-001", publishedVersion=2
            ),
        )

    def test_discloses_the_published_snapshots_skills(self, app, make_user, catalog):
        version = AgentVersion(
            agentId="ast-001",
            version=2,
            name="Policy Lookup",
            description="Find and cite university policy",
            instructions="Answer from the policy manual.",
            bindings=_skill_bindings("skill-a"),
        )
        mock_auth_user(app, make_user())
        with _as(self._published("skill-a", "skill-c")), patch(
            f"{RESOLUTION_MODULE}.get_version", new_callable=AsyncMock, return_value=version
        ):
            resp = TestClient(app).get(URL)

        # ``skill-c`` is on the draft only. It reaches recipients when a new version is
        # approved, and the submit dialog discloses it then.
        assert [s["ref"] for s in resp.json()["exposedSkills"]] == ["skill-a"]

    def test_falls_back_to_the_draft_when_the_snapshot_is_missing(self, app, make_user, catalog):
        """Over-warning the owner is the safe direction; an empty list would not be."""
        mock_auth_user(app, make_user())
        with _as(self._published("skill-a", "skill-c")), patch(
            f"{RESOLUTION_MODULE}.get_version", new_callable=AsyncMock, return_value=None
        ):
            resp = TestClient(app).get(URL)

        assert resp.status_code == 200
        assert sorted(s["ref"] for s in resp.json()["exposedSkills"]) == ["skill-a", "skill-c"]


class TestWhoMayAsk:
    @pytest.mark.parametrize("permission", ["editor", "viewer"])
    def test_only_the_owner(self, app, make_user, catalog, permission):
        """Sharing is owner-only, so its disclosure is too."""
        mock_auth_user(app, make_user())
        with _as(_make_assistant(bindings=_skill_bindings("skill-a")), permission):
            resp = TestClient(app).get(URL)

        assert resp.status_code == 403
        catalog.batch_get_skills.assert_not_called()

    def test_missing_agent_is_404(self, app, make_user):
        mock_auth_user(app, make_user())
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            assert TestClient(app).get(URL).status_code == 404

    def test_a_project_harness_exposes_nothing(self, app, make_user, catalog):
        """A harness has no shares — its audience is the project's membership."""
        mock_auth_user(app, make_user())
        with _as(_make_assistant(kind="project", bindings=_skill_bindings("skill-a"))):
            resp = TestClient(app).get(URL)

        assert resp.json()["exposedSkills"] == []

    def test_404_when_the_agent_surface_is_off(self, app, make_user, monkeypatch):
        monkeypatch.setenv("AGENTS_API_ENABLED", "false")
        mock_auth_user(app, make_user())
        assert TestClient(app).get(URL).status_code == 404

    def test_not_captured_by_the_agent_id_path_param(self, app, make_user, catalog):
        """`/{agent_id}/shares/exposed-skills` must not resolve as `GET /{agent_id}`."""
        mock_auth_user(app, make_user())
        with _as(_make_assistant(bindings=[])):
            resp = TestClient(app).get(URL)

        assert "exposedSkills" in resp.json()

