"""Managed-model field resolution for the OpenAI-compatible Bedrock surfaces.

``apiMode`` and ``region`` are wire-generic fields that mean something on two
providers — ``mantle`` and ``bedrock-responses`` — and nothing on the rest.
These pin how a record is normalized on write, which is the only place that
decision is made.
"""

import pytest

from apis.shared.models.managed_models import (
    _resolve_supports_caching,
    _resolve_mantle_api_mode,
    _resolve_mantle_region,
)


class TestApiModeResolution:
    @pytest.mark.parametrize(
        "stored,expected",
        [("chat", "chat"), ("responses", "responses"), (None, "chat"), ("bogus", "chat")],
    )
    def test_mantle_is_admin_selectable(self, stored, expected):
        assert _resolve_mantle_api_mode(stored, "mantle") == expected

    @pytest.mark.parametrize("stored", ["chat", "responses", None, "", "bogus"])
    def test_bedrock_responses_is_always_responses(self, stored):
        """Not a choice: that transport exists because 5.6 caches only there.

        A stored 'chat' — from a hand-edited record, or a provider switch that
        left the old value behind — would silently downgrade the model to an
        uncached Chat Completions call. Normalize, don't honor.
        """
        assert _resolve_mantle_api_mode(stored, "bedrock-responses") == "responses"

    @pytest.mark.parametrize("provider", ["bedrock", "openai", "gemini"])
    def test_inert_for_other_providers(self, provider):
        assert _resolve_mantle_api_mode("responses", provider) is None

    def test_provider_matching_is_case_insensitive(self):
        assert _resolve_mantle_api_mode("chat", "Bedrock-Responses") == "responses"


class TestRegionResolution:
    @pytest.mark.parametrize("provider", ["mantle", "bedrock-responses"])
    def test_kept_on_both_openai_surfaces(self, provider):
        assert _resolve_mantle_region("us-east-1", provider) == "us-east-1"

    @pytest.mark.parametrize("provider", ["mantle", "bedrock-responses"])
    def test_empty_means_the_apps_region(self, provider):
        assert _resolve_mantle_region("", provider) is None
        assert _resolve_mantle_region(None, provider) is None

    @pytest.mark.parametrize("provider", ["bedrock", "openai", "gemini"])
    def test_dropped_for_other_providers(self, provider):
        assert _resolve_mantle_region("us-east-1", provider) is None


class TestCachingDefault:
    @pytest.mark.parametrize("provider", ["bedrock", "bedrock-responses"])
    def test_defaults_on_for_caching_families(self, provider):
        assert _resolve_supports_caching(None, provider) is True

    def test_mantle_defaults_off(self):
        """Mantle hosts open-weight models that mostly don't cache."""
        assert _resolve_supports_caching(None, "mantle") is False

    @pytest.mark.parametrize("provider", ["openai", "gemini"])
    def test_other_providers_default_off(self, provider):
        assert _resolve_supports_caching(None, provider) is False

    @pytest.mark.parametrize("provider", ["bedrock", "bedrock-responses", "mantle"])
    def test_explicit_value_always_wins(self, provider):
        assert _resolve_supports_caching(False, provider) is False
        assert _resolve_supports_caching(True, provider) is True
