"""Tests for the chat-mode policy + per-turn skill selection on /invocations.

Covers (skills-mode PR-2, docs/specs/skills-mode.md):
- _resolve_effective_agent_type — admin policy vs. client agent_type
- _apply_enabled_skills_filter — RBAC ∩ client enabled_skills
- route-level threading of both into get_agent
- PausedTurnSnapshot.enabled_skills round-trip
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api.chat.routes import (
    _apply_enabled_skills_filter,
    _resolve_effective_agent_type,
    router,
)
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.platform_settings.models import ChatModeSettings


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/routes/test_inference.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def trusted_user(make_user):
    return make_user(raw_token="fake-jwt-token")


@pytest.fixture
def authed_client(app, trusted_user):
    app.dependency_overrides[get_current_user_trusted] = lambda: trusted_user
    return TestClient(app)


@pytest.fixture(autouse=True)
def _skills_enabled(monkeypatch):
    """This module exercises skills-mode behavior, so run with the feature on.
    The disabled-feature override (force chat) is covered by its own test that
    flips this back off."""
    monkeypatch.setenv("SKILLS_ENABLED", "true")


class _StubSettingsService:
    def __init__(self, settings: ChatModeSettings):
        self._settings = settings

    async def get_settings(self) -> ChatModeSettings:
        return self._settings


def _mock_agent():
    agent = MagicMock()

    async def fake_stream(*args, **kwargs):
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    return agent


# ---------------------------------------------------------------------------
# _resolve_effective_agent_type
# ---------------------------------------------------------------------------


class TestResolveEffectiveAgentType:
    def test_toggle_allowed_honors_client_choice(self):
        settings = ChatModeSettings(default_mode="skill", allow_mode_toggle=True)
        assert _resolve_effective_agent_type("chat", settings) == "chat"
        assert _resolve_effective_agent_type("skill", settings) == "skill"

    def test_omitted_falls_back_to_admin_default(self):
        assert (
            _resolve_effective_agent_type(
                None, ChatModeSettings(default_mode="skill", allow_mode_toggle=True)
            )
            == "skill"
        )
        assert (
            _resolve_effective_agent_type(
                None, ChatModeSettings(default_mode="chat", allow_mode_toggle=False)
            )
            == "chat"
        )

    def test_toggle_disabled_overrides_client_choice(self):
        settings = ChatModeSettings(default_mode="skill", allow_mode_toggle=False)
        assert _resolve_effective_agent_type("chat", settings) == "skill"

        settings = ChatModeSettings(default_mode="chat", allow_mode_toggle=False)
        assert _resolve_effective_agent_type("skill", settings) == "chat"

    def test_toggle_disabled_matching_choice_passes(self):
        settings = ChatModeSettings(default_mode="skill", allow_mode_toggle=False)
        assert _resolve_effective_agent_type("skill", settings) == "skill"

    def test_non_mode_types_bypass_the_policy(self):
        settings = ChatModeSettings(default_mode="skill", allow_mode_toggle=False)
        assert _resolve_effective_agent_type("voice", settings) == "voice"


# ---------------------------------------------------------------------------
# _apply_enabled_skills_filter
# ---------------------------------------------------------------------------


class TestApplyEnabledSkillsFilter:
    def test_none_means_all_accessible(self):
        assert _apply_enabled_skills_filter(["a", "b"], None) == ["a", "b"]

    def test_intersection_preserves_accessible_order(self):
        assert _apply_enabled_skills_filter(["a", "b", "c"], ["c", "a"]) == ["a", "c"]

    def test_client_cannot_grant_inaccessible_skills(self):
        assert _apply_enabled_skills_filter(["a"], ["a", "forbidden"]) == ["a"]

    def test_empty_selection_yields_zero_skills(self):
        assert _apply_enabled_skills_filter(["a", "b"], []) == []

    def test_empty_accessible_stays_empty(self):
        assert _apply_enabled_skills_filter([], ["a"]) == []


# ---------------------------------------------------------------------------
# Route-level threading into get_agent
# ---------------------------------------------------------------------------


class TestInvocationsModePolicy:
    def _invoke(self, authed_client, payload, *, settings, accessible):
        get_agent_mock = MagicMock(return_value=_mock_agent())
        resolve_mock = AsyncMock(return_value=accessible)
        with patch(
            "apis.inference_api.chat.routes.get_agent", get_agent_mock
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes._resolve_accessible_skill_ids",
            resolve_mock,
        ), patch(
            "apis.inference_api.chat.routes.get_chat_mode_settings_service",
            return_value=_StubSettingsService(settings),
        ):
            resp = authed_client.post("/invocations", json=payload)
            _ = resp.text  # force the streaming generator to run

        assert resp.status_code == 200
        return get_agent_mock.call_args.kwargs

    def test_enabled_skills_narrows_the_skill_set(self, authed_client):
        kwargs = self._invoke(
            authed_client,
            {
                "session_id": "sess-1",
                "message": "hi",
                "enabled_skills": ["pdf_workflows", "forbidden_skill"],
            },
            settings=ChatModeSettings(default_mode="skill", allow_mode_toggle=True),
            accessible=["web_research", "pdf_workflows"],
        )
        assert kwargs["agent_type"] == "skill"
        assert kwargs["accessible_skill_ids"] == ["pdf_workflows"]

    def test_omitted_enabled_skills_keeps_all_accessible(self, authed_client):
        kwargs = self._invoke(
            authed_client,
            {"session_id": "sess-2", "message": "hi"},
            settings=ChatModeSettings(default_mode="skill", allow_mode_toggle=True),
            accessible=["web_research"],
        )
        assert kwargs["accessible_skill_ids"] == ["web_research"]

    def test_toggle_disabled_forces_admin_default(self, authed_client):
        kwargs = self._invoke(
            authed_client,
            {"session_id": "sess-3", "message": "hi", "agent_type": "chat"},
            settings=ChatModeSettings(default_mode="skill", allow_mode_toggle=False),
            accessible=["web_research"],
        )
        # Client asked for chat; policy forces the skill default through.
        assert kwargs["agent_type"] == "skill"
        assert kwargs["accessible_skill_ids"] == ["web_research"]

    def test_admin_default_chat_skips_skill_resolution(self, authed_client):
        get_agent_mock = MagicMock(return_value=_mock_agent())
        resolve_mock = AsyncMock(return_value=["web_research"])
        with patch(
            "apis.inference_api.chat.routes.get_agent", get_agent_mock
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes._resolve_accessible_skill_ids",
            resolve_mock,
        ), patch(
            "apis.inference_api.chat.routes.get_chat_mode_settings_service",
            return_value=_StubSettingsService(
                ChatModeSettings(default_mode="chat", allow_mode_toggle=True)
            ),
        ):
            resp = authed_client.post(
                "/invocations", json={"session_id": "sess-4", "message": "hi"}
            )
            _ = resp.text

        assert resp.status_code == 200
        resolve_mock.assert_not_awaited()
        kwargs = get_agent_mock.call_args.kwargs
        assert kwargs["agent_type"] == "chat"
        assert kwargs["accessible_skill_ids"] is None

    def test_skills_disabled_forces_chat_over_skill_request(
        self, authed_client, monkeypatch
    ):
        # Feature off + client explicitly asks for skill + policy allows it:
        # the turn must still route through the ChatAgent with no skills.
        monkeypatch.setenv("SKILLS_ENABLED", "false")
        get_agent_mock = MagicMock(return_value=_mock_agent())
        resolve_mock = AsyncMock(return_value=["web_research"])
        with patch(
            "apis.inference_api.chat.routes.get_agent", get_agent_mock
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes._resolve_accessible_skill_ids",
            resolve_mock,
        ), patch(
            "apis.inference_api.chat.routes.get_chat_mode_settings_service",
            return_value=_StubSettingsService(
                ChatModeSettings(default_mode="skill", allow_mode_toggle=True)
            ),
        ):
            resp = authed_client.post(
                "/invocations",
                json={
                    "session_id": "sess-off",
                    "message": "hi",
                    "agent_type": "skill",
                },
            )
            _ = resp.text

        assert resp.status_code == 200
        resolve_mock.assert_not_awaited()
        kwargs = get_agent_mock.call_args.kwargs
        assert kwargs["agent_type"] == "chat"
        assert kwargs["accessible_skill_ids"] is None


# ---------------------------------------------------------------------------
# PausedTurnSnapshot.enabled_skills
# ---------------------------------------------------------------------------


class TestSnapshotEnabledSkills:
    def test_round_trips_through_alias(self):
        from apis.shared.sessions.models import PausedTurnSnapshot

        snap = PausedTurnSnapshot(
            agent_type="skill",
            enabled_skills=["web_research"],
            captured_at="2026-06-11T12:00:00+00:00",
            expires_at="2026-06-11T13:00:00+00:00",
        )
        dumped = snap.model_dump(by_alias=True)
        assert dumped["enabledSkills"] == ["web_research"]

        restored = PausedTurnSnapshot.model_validate(dumped)
        assert restored.enabled_skills == ["web_research"]

    def test_legacy_snapshot_defaults_to_none(self):
        from apis.shared.sessions.models import PausedTurnSnapshot

        snap = PausedTurnSnapshot.model_validate(
            {
                "agentType": "skill",
                "capturedAt": "2026-06-11T12:00:00+00:00",
                "expiresAt": "2026-06-11T13:00:00+00:00",
            }
        )
        assert snap.enabled_skills is None
