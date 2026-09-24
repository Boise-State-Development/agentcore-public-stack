"""The model a turn runs on when nothing names one (model-retirement spec §6, follow-up 3).

The chain is: request → Agent binding → the user's saved default → the catalog's
``isDefault`` row → the hard-coded ``Defaults.MODEL_ID``. Only a catalog row has
pricing, so every link before the last must resolve to one: a turn that reaches
an id with no row is unmetered and free against quota. Prod reached it on every
scheduled run and every Agent without a ``modelConfig``, because the hard-coded
id is ``us.*`` and prod registers ``global.*``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from apis.inference_api.chat import routes as chat_routes

USER = SimpleNamespace(user_id="u-1", email="u@example.edu", roles=[])


def _catalog_default(model_id, provider="bedrock"):
    async def get_default():
        if model_id is None:
            return None
        return SimpleNamespace(model_id=model_id, provider=provider)

    return get_default


def _saved_default(model_id, provider="bedrock"):
    async def resolve(_user_id, settings=None):
        return (model_id, provider if model_id else None)

    return resolve


class _Rbac:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.checked: list[str] = []

    async def can_access_model(self, _user, model_id):
        self.checked.append(model_id)
        return self.allowed


@pytest.fixture
def rbac(monkeypatch):
    service = _Rbac(allowed=True)
    monkeypatch.setattr(chat_routes, "get_app_role_service", lambda: service)
    return service


def _resolve(provider=None):
    return asyncio.run(chat_routes._resolve_fallback_model("u-1", USER, provider, settings={}))


def test_the_users_saved_default_wins(monkeypatch, rbac):
    monkeypatch.setattr(chat_routes, "_resolve_user_default_model", _saved_default("global.sonnet"))
    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default("global.haiku"))

    assert _resolve() == ("global.sonnet", "bedrock")
    assert rbac.checked == ["global.sonnet"]


def test_no_saved_default_runs_the_catalog_default(monkeypatch, rbac):
    monkeypatch.setattr(chat_routes, "_resolve_user_default_model", _saved_default(None))
    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default("global.sonnet-5"))

    assert _resolve() == ("global.sonnet-5", "bedrock")


def test_a_saved_default_rbac_denies_falls_to_the_catalog_default(monkeypatch, rbac):
    rbac.allowed = False
    monkeypatch.setattr(chat_routes, "_resolve_user_default_model", _saved_default("global.opus"))
    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default("global.sonnet-5"))

    assert _resolve() == ("global.sonnet-5", "bedrock")


def test_the_catalog_default_is_not_rbac_gated(monkeypatch, rbac):
    # The SPA sends model_id: null only for a user who can see no enabled model.
    # Gating the catalog default would send exactly those turns back to the
    # unpriced hard-coded id — the bug this fallback exists to close.
    rbac.allowed = False
    monkeypatch.setattr(chat_routes, "_resolve_user_default_model", _saved_default(None))
    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default("global.sonnet-5"))

    assert _resolve() == ("global.sonnet-5", "bedrock")
    assert rbac.checked == []


def test_the_catalog_defaults_provider_replaces_the_requests(monkeypatch, rbac):
    # The request's provider described no model; a default on another transport
    # misroutes to ConverseStream without its own.
    monkeypatch.setattr(chat_routes, "_resolve_user_default_model", _saved_default(None))
    monkeypatch.setattr(
        chat_routes, "get_default_managed_model", _catalog_default("global.openai.gpt-5.6-sol", "bedrock-responses")
    )

    assert _resolve(provider="bedrock") == ("global.openai.gpt-5.6-sol", "bedrock-responses")


def test_no_catalog_default_leaves_the_hard_coded_fallback(monkeypatch, rbac, caplog):
    monkeypatch.setattr(chat_routes, "_resolve_user_default_model", _saved_default(None))
    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default(None))

    with caplog.at_level("WARNING"):
        assert _resolve(provider="bedrock") == (None, "bedrock")
    assert "no enabled default" in caplog.text


def test_a_saved_default_with_no_catalog_row_is_treated_as_unset(monkeypatch):
    # A wildcard grant passes the RBAC re-check for any id, so without this a
    # deleted model kept running — with no pricing row, unmetered.
    async def no_row(_model_id):
        return None

    monkeypatch.setattr(chat_routes, "_find_managed_model", no_row)
    assert asyncio.run(
        chat_routes._resolve_user_default_model("u-1", settings={"defaultModelId": "us.deleted-model"})
    ) == (None, None)


def test_the_system_default_reads_the_catalog(monkeypatch):
    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default("global.sonnet-5"))
    assert asyncio.run(chat_routes._resolve_system_default_model()) == ("global.sonnet-5", "bedrock")

    monkeypatch.setattr(chat_routes, "get_default_managed_model", _catalog_default(None))
    assert asyncio.run(chat_routes._resolve_system_default_model()) == (None, None)
