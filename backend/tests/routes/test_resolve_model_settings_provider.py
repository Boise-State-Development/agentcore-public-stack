"""Regression tests for provider recovery in `_resolve_model_settings`.

Agent (assistant) model bindings persist only `model_id` — never `provider`
(see `AgentModelConfig`). When such an agent is previewed/invoked, the request
also carries no provider, so without server-side recovery a Mantle model like
`openai.gpt-5.4` resolves to provider=None → Bedrock and fails in ConverseStream
with "The provided model identifier is invalid" — even though the same model
works from the normal chat path (which always sends `provider` with `model_id`).

`_resolve_model_settings` therefore returns the model's registered provider so
the caller can backfill it. These tests pin that contract.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import routes


def _managed(**kwargs):
    """Minimal managed-model stand-in for the resolver's attribute reads."""
    defaults = dict(
        model_id="openai.gpt-5.4",
        provider="mantle",
        supports_caching=False,
        mantle_api_mode="responses",
        mantle_region=None,
        supported_params=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_returns_provider_for_mantle_model():
    with patch.object(
        routes, "_find_managed_model", AsyncMock(return_value=_managed())
    ):
        (
            caching,
            _params,
            mantle_api_mode,
            mantle_region,
            provider,
        ) = await routes._resolve_model_settings(
            model_id="openai.gpt-5.4",
            explicit_caching_enabled=None,
            request_inference_params=None,
        )

    assert provider == "mantle"
    assert mantle_api_mode == "responses"
    assert mantle_region is None
    assert caching is False


@pytest.mark.asyncio
async def test_provider_none_when_model_unknown():
    with patch.object(routes, "_find_managed_model", AsyncMock(return_value=None)):
        _, _, _, _, provider = await routes._resolve_model_settings(
            model_id="openai.gpt-5.4",
            explicit_caching_enabled=None,
            request_inference_params=None,
        )
    assert provider is None


@pytest.mark.asyncio
async def test_provider_none_when_no_model_id():
    # No registry lookup happens without a model id; provider stays None.
    with patch.object(routes, "_find_managed_model", AsyncMock()) as find:
        _, _, _, _, provider = await routes._resolve_model_settings(
            model_id=None,
            explicit_caching_enabled=None,
            request_inference_params=None,
        )
    assert provider is None
    find.assert_not_awaited()
