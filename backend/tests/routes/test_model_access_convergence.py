"""One model-access rule across every surface (#798).

``AppRoleService.can_access_model`` (chat turn, api-converse, an Agent's model
override) once read role grants only, while ``ModelAccessService`` (the
``/models`` catalog, Agent Designer validation) also read ``enabled`` and the
legacy ``availableToRoles`` list, and ``account_tools`` kept a third copy. A
legacy-granted model was listed and then refused; a disabled one was hidden and
still ran. These tests pin every surface to ``grants_model_access``.
"""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.services.model_access import ModelAccessService
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel
from apis.shared.models.retirement import resolve_from_catalog
from apis.shared.rbac.model_access import grants_model_access
from apis.shared.rbac.service import AppRoleService

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
LOOKUP = "apis.shared.models.managed_models.find_managed_model_by_model_id"


def _row(
    model_id: str,
    *,
    enabled: bool = True,
    available_to_roles=None,
    status: str = "active",
    replaced_by=None,
) -> ManagedModel:
    return ManagedModel(
        id=f"uuid-{model_id}", modelId=model_id, modelName=model_id, provider="bedrock",
        providerName="Amazon Bedrock", inputModalities=["text"], outputModalities=["text"],
        maxInputTokens=200000, enabled=enabled, availableToRoles=available_to_roles or [],
        inputPricePerMillionTokens=1.0, outputPricePerMillionTokens=5.0, status=status,
        replacedBy=replaced_by, createdAt=NOW, updatedAt=NOW,
    )


def _user(roles=("Faculty",)) -> User:
    return User(email="u@example.com", user_id="user-001", name="U", roles=list(roles), raw_token="t")


def _role_service(granted) -> AppRoleService:
    """A real AppRoleService whose role resolution returns ``granted`` model ids."""
    svc = AppRoleService(repository=MagicMock(), cache=MagicMock())
    svc.resolve_user_permissions = AsyncMock(return_value=SimpleNamespace(models=list(granted)))
    return svc


# The catalog every surface below reads.
LEGACY = _row("legacy-model", available_to_roles=["Faculty"])
DISABLED = _row("disabled-model", enabled=False)
DISABLED_LEGACY = _row("disabled-legacy", enabled=False, available_to_roles=["Faculty"])
GRANTED = _row("granted-model")
UNGRANTED = _row("ungranted-model", available_to_roles=["Staff"])
CATALOG = [LEGACY, DISABLED, DISABLED_LEGACY, GRANTED, UNGRANTED]


class TestPredicate:
    def test_disabled_refused_even_for_wildcard(self):
        assert grants_model_access("disabled-model", DISABLED, {"*"}, set()) is False

    def test_role_grant(self):
        assert grants_model_access("granted-model", GRANTED, {"granted-model"}, set()) is True

    def test_wildcard(self):
        assert grants_model_access("ungranted-model", UNGRANTED, {"*"}, set()) is True

    def test_legacy_available_to_roles(self):
        assert grants_model_access("legacy-model", LEGACY, set(), {"Faculty"}) is True
        assert grants_model_access("ungranted-model", UNGRANTED, set(), {"Faculty"}) is False

    def test_no_catalog_row_is_role_grants_only(self):
        assert grants_model_access("uncurated", None, {"uncurated"}, set()) is True
        assert grants_model_access("uncurated", None, {"*"}, set()) is True
        assert grants_model_access("uncurated", None, set(), {"Faculty"}) is False


class TestEverySurfaceAgrees:
    """For each grant shape, the single check, both catalog filters and the
    catalog service's single check give the same answer for every row."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("granted", [[], ["granted-model"], ["*"], ["disabled-model"]])
    async def test_agreement(self, granted):
        user = _user()
        role_svc = _role_service(granted)
        catalog_svc = ModelAccessService(app_role_service=role_svc)

        listed_by_catalog = {m.model_id for m in await catalog_svc.filter_accessible_models(user, CATALOG)}
        listed_by_rbac = {m.model_id for m in await role_svc.filter_accessible_models(user, CATALOG)}
        assert listed_by_catalog == listed_by_rbac

        for row in CATALOG:
            expected = row.model_id in listed_by_catalog
            assert await catalog_svc.can_access_model(user, row) is expected, row.model_id
            # With the row handed over (the turn path) ...
            assert await role_svc.can_access_model(user, row.model_id, record=row) is expected, row.model_id
            # ... and with the row looked up in the catalog (the fallback-default path).
            with patch(LOOKUP, AsyncMock(return_value=row)):
                assert await role_svc.can_access_model(user, row.model_id) is expected, row.model_id

    @pytest.mark.asyncio
    async def test_the_two_divergences_from_798_are_closed(self):
        user = _user()
        role_svc = _role_service([])
        assert await role_svc.can_access_model(user, "legacy-model", record=LEGACY) is True
        assert await _role_service(["*"]).can_access_model(user, "disabled-model", record=DISABLED) is False

    @pytest.mark.asyncio
    async def test_unreadable_catalog_falls_back_to_role_grants(self):
        user = _user()
        with patch(LOOKUP, AsyncMock(side_effect=RuntimeError("no table"))):
            assert await _role_service(["granted-model"]).can_access_model(user, "granted-model") is True
            assert await _role_service([]).can_access_model(user, "legacy-model") is False

    @pytest.mark.asyncio
    async def test_set_default_model_tool_uses_the_same_rule(self):
        from agents.local_tools import account_tools

        role_svc = _role_service([])
        with patch("apis.shared.models.managed_models.list_all_managed_models", AsyncMock(return_value=CATALOG)), \
             patch("apis.shared.rbac.service.get_app_role_service", return_value=role_svc):
            picked = {m.model_id for m in await account_tools._accessible_models(_user())}
        assert picked == {"legacy-model"}


# ---------------------------------------------------------------------------
# Entry points, with a real AppRoleService behind them
# ---------------------------------------------------------------------------

RETIREMENT_CATALOG = CATALOG + [
    # A retired row is often disabled too; the redirect must still run, and the
    # check must judge the successor — never the retired row's own flag.
    _row("old-disabled", enabled=False, status="retired", replaced_by="granted-model"),
    _row("old-to-disabled", status="retired", replaced_by="disabled-model"),
]


async def _resolve(model_id):
    return resolve_from_catalog(model_id, RETIREMENT_CATALOG) if model_id else None


def _spy(svc: AppRoleService) -> list:
    """Record what the real ``can_access_model`` answers, past the route's own handling."""
    answers: list = []
    real = svc.can_access_model

    async def spy(*args, **kwargs):
        answer = await real(*args, **kwargs)
        answers.append((args[1], answer))
        return answer

    svc.can_access_model = spy
    return answers


class TestInvocations:
    def _post(self, model_id: str, svc: AppRoleService):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _user
        lookup = AsyncMock(side_effect=AssertionError("turn path must reuse the resolved row"))
        with patch("apis.inference_api.chat.routes.resolve_effective_model", _resolve), \
             patch("apis.inference_api.chat.routes.get_app_role_service", return_value=svc), \
             patch("apis.inference_api.chat.routes.is_quota_enforcement_enabled", return_value=False), \
             patch(LOOKUP, lookup):
            resp = TestClient(app, raise_server_exceptions=False).post(
                "/invocations",
                json={"session_id": "s-1", "message": "hi", "model_id": model_id, "provider": "bedrock"},
            )
        lookup.assert_not_awaited()
        return resp

    def test_disabled_model_refused(self):
        resp = self._post("disabled-model", _role_service(["*"]))
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Access denied to model: disabled-model"

    def test_legacy_grant_allowed(self):
        svc = _role_service([])
        answers = _spy(svc)
        resp = self._post("legacy-model", svc)
        assert answers == [("legacy-model", True)]
        assert resp.status_code != 403

    def test_retired_disabled_row_still_redirects_to_its_successor(self):
        svc = _role_service(["granted-model"])
        answers = _spy(svc)
        resp = self._post("old-disabled", svc)
        assert answers == [("granted-model", True)]
        assert resp.status_code != 403

    def test_redirect_to_a_disabled_successor_is_refused(self):
        resp = self._post("old-to-disabled", _role_service(["*"]))
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Access denied to model: disabled-model"


class TestApiConverse:
    def _post(self, model_id: str, svc: AppRoleService):
        from apis.app_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)
        key = MagicMock(user_id="user-001", key_id="key-001")
        key.name = "Test Key"
        limiter = MagicMock()
        limiter.check_rate_limit = AsyncMock(return_value=True)
        bedrock = MagicMock()
        bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "hello"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }
        with patch("apis.app_api.chat.converse_routes._validate_api_key", new_callable=AsyncMock, return_value=key), \
             patch("apis.app_api.chat.converse_routes._build_user_from_api_key", new_callable=AsyncMock, return_value=_user()), \
             patch("apis.app_api.chat.converse_routes.get_app_role_service", return_value=svc), \
             patch("apis.app_api.chat.converse_routes.resolve_effective_model", _resolve), \
             patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=bedrock), \
             patch("apis.app_api.chat.converse_routes._record_cost", new_callable=AsyncMock), \
             patch("apis.shared.rate_limit.get_rate_limiter", return_value=limiter), \
             patch("apis.shared.quota.is_quota_enforcement_enabled", return_value=False), \
             patch(LOOKUP, AsyncMock(side_effect=AssertionError("must reuse the resolved row"))):
            return TestClient(app, raise_server_exceptions=False).post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={"model_id": model_id, "messages": [{"role": "user", "content": "hi"}]},
            )

    def test_legacy_grant_allowed(self):
        assert self._post("legacy-model", _role_service([])).status_code == 200

    def test_disabled_model_refused(self):
        resp = self._post("disabled-model", _role_service(["*"]))
        assert resp.status_code == 403

    def test_retired_disabled_row_still_redirects_to_its_successor(self):
        resp = self._post("old-disabled", _role_service(["granted-model"]))
        assert resp.status_code == 200
        assert resp.json()["model_id"] == "granted-model"


class TestAgentModelOverride:
    """The run-time override agrees with what design-time validation (the catalog) admits."""

    MODULE = "apis.inference_api.chat.agent_binding_resolver"

    async def _resolve_override(self, monkeypatch, model_id: str, svc: AppRoleService):
        from apis.inference_api.chat.agent_binding_resolver import (
            AgentBindingBlockedError,
            resolve_agent_invocation,
        )
        from apis.shared.assistants.models import AgentModelConfig, Assistant

        monkeypatch.setattr(f"{self.MODULE}.resolve_effective_model", _resolve)
        monkeypatch.setattr(f"{self.MODULE}.get_app_role_service", lambda: svc)
        assistant = Assistant(
            assistantId="ast-1", ownerId="u-alice", ownerName="Alice", name="A", description="d",
            instructions="i", vectorIndexId="idx", visibility="SHARED", createdAt="t", updatedAt="t",
            status="COMPLETE", model_settings=AgentModelConfig(model_id=model_id),
        )
        try:
            plan = await resolve_agent_invocation(assistant, _user())
        except AgentBindingBlockedError:
            return None
        return plan.model_override.model_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("granted", [[], ["granted-model"], ["*"]])
    async def test_override_matches_catalog(self, monkeypatch, granted):
        svc = _role_service(granted)
        listed = {m.model_id for m in await ModelAccessService(app_role_service=svc).filter_accessible_models(_user(), CATALOG)}
        with patch(LOOKUP, AsyncMock(side_effect=AssertionError("must reuse the resolved row"))):
            for row in CATALOG:
                runs = await self._resolve_override(monkeypatch, row.model_id, svc)
                assert (runs == row.model_id) is (row.model_id in listed), row.model_id

    @pytest.mark.asyncio
    async def test_retired_disabled_row_redirects(self, monkeypatch):
        svc = _role_service(["granted-model"])
        assert await self._resolve_override(monkeypatch, "old-disabled", svc) == "granted-model"
