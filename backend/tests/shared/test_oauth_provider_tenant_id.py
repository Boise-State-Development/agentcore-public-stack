"""Validation of a Microsoft connector's Entra tenant on the request models.

The tenant is placed in the authority URL AgentCore sends users to for
consent, so a malformed or whitespace-padded value only fails later, as an
Entra error page in the consent popup. These tests pin that it fails at the
admin API instead.
"""

import pytest
from pydantic import ValidationError

from apis.shared.oauth.models import (
    OAuthProvider,
    OAuthProviderCreate,
    OAuthProviderType,
    OAuthProviderUpdate,
)

_TENANT_ID = "11111111-2222-3333-4444-555555555555"


def _create(**overrides) -> OAuthProviderCreate:
    data = dict(
        provider_id="azure-devops",
        display_name="Azure DevOps",
        provider_type=OAuthProviderType.MICROSOFT,
        client_id="cid",
        client_secret="sec",
    )
    data.update(overrides)
    return OAuthProviderCreate(**data)


class TestCreateTenantId:
    @pytest.mark.parametrize("tenant", [_TENANT_ID, "contoso.onmicrosoft.com"])
    def test_accepts_guid_or_domain(self, tenant):
        assert _create(tenant_id=tenant).tenant_id == tenant

    def test_strips_surrounding_whitespace(self):
        assert _create(tenant_id=f"  {_TENANT_ID}\n").tenant_id == _TENANT_ID

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_is_unset(self, blank):
        assert _create(tenant_id=blank).tenant_id is None

    @pytest.mark.parametrize(
        "bad", ["https://login.microsoftonline.com/x", "a b", "tenant/../x", "-lead"]
    )
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValidationError, match="tenant_id"):
            _create(tenant_id=bad)

    def test_rejects_non_microsoft(self):
        with pytest.raises(ValidationError, match="only valid for microsoft"):
            _create(provider_type=OAuthProviderType.GOOGLE, tenant_id=_TENANT_ID)


class TestUpdateTenantId:
    def test_none_means_unchanged(self):
        assert OAuthProviderUpdate().tenant_id is None

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_blank_means_clear(self, blank):
        assert OAuthProviderUpdate(tenant_id=blank).tenant_id == ""

    def test_strips_and_validates(self):
        assert OAuthProviderUpdate(tenant_id=f" {_TENANT_ID} ").tenant_id == _TENANT_ID
        with pytest.raises(ValidationError):
            OAuthProviderUpdate(tenant_id="not a tenant")


def test_tenant_round_trips_through_dynamo():
    provider = OAuthProvider(
        provider_id="azure-devops",
        display_name="Azure DevOps",
        provider_type=OAuthProviderType.MICROSOFT,
        scopes=[],
        allowed_roles=[],
        tenant_id=_TENANT_ID,
    )
    item = provider.to_dynamo_item()
    assert item["tenantId"] == _TENANT_ID
    assert OAuthProvider.from_dynamo_item(item).tenant_id == _TENANT_ID
