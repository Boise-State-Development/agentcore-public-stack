"""DynamoDB serde tests for `OAuthProvider`.

Focused on the round-trip — admins paste vendor-specific OAuth params
into the form, we persist them, and they must come back unchanged so
the merge with the hardcoded baseline at runtime stays correct.
"""

from __future__ import annotations

import pytest

from apis.shared.oauth.models import OAuthProvider, OAuthProviderType


def _provider(**overrides) -> OAuthProvider:
    base = dict(
        provider_id="google-workspace",
        display_name="Google Workspace",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["openid", "email"],
        allowed_roles=[],
    )
    base.update(overrides)
    return OAuthProvider(**base)


class TestCustomParametersRoundTrip:
    def test_round_trip_preserves_custom_parameters(self) -> None:
        original = _provider(
            custom_parameters={"hd": "mycompany.com", "prompt": "consent"},
        )
        revived = OAuthProvider.from_dynamo_item(original.to_dynamo_item())
        assert revived.custom_parameters == {
            "hd": "mycompany.com",
            "prompt": "consent",
        }

    def test_none_round_trips_as_none(self) -> None:
        # Default state for vendors with no admin-supplied extras.
        original = _provider(custom_parameters=None)
        item = original.to_dynamo_item()
        # Persisted as None — the route layer treats `{}` as "explicitly
        # cleared" and converts it to None before save.
        assert item["customParameters"] is None
        revived = OAuthProvider.from_dynamo_item(item)
        assert revived.custom_parameters is None

    def test_legacy_item_without_field_loads_as_none(self) -> None:
        # Simulates a pre-migration row in DynamoDB. New code must not
        # KeyError on the missing attribute.
        item = _provider(custom_parameters={"foo": "bar"}).to_dynamo_item()
        del item["customParameters"]
        revived = OAuthProvider.from_dynamo_item(item)
        assert revived.custom_parameters is None
