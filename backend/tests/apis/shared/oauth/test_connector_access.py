"""The connector visibility rule: allowed roles, held directly or inherited."""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from apis.shared.oauth.connector_access import (
    connector_role_ids,
    connector_visible_to_user,
)
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.rbac.models import AppRole, UserEffectivePermissions


def _provider(allowed_roles: List[str], *, enabled: bool = True) -> OAuthProvider:
    return OAuthProvider(
        provider_id="contract-salesforce",
        display_name="Salesforce",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["api"],
        allowed_roles=allowed_roles,
        enabled=enabled,
    )


def _role(
    role_id: str, *, inherits_from: Optional[List[str]] = None, enabled: bool = True
) -> AppRole:
    return AppRole(
        role_id=role_id,
        display_name=role_id,
        description="",
        inherits_from=inherits_from or [],
        enabled=enabled,
    )


def _permissions(app_roles: List[str]) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id="u1",
        app_roles=app_roles,
        tools=[],
        models=[],
        quota_tier=None,
        resolved_at="2026-11-12T00:00:00Z",
    )


class _Roles:
    """The one method of the role service this module reads."""

    def __init__(self, roles: Dict[str, AppRole]):
        self._roles = roles
        self.reads: List[str] = []

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        self.reads.append(role_id)
        return self._roles.get(role_id)


class TestConnectorVisibleToUser:
    def test_unrestricted_connector_is_visible_to_anyone(self):
        assert connector_visible_to_user(_provider([]), [])

    def test_disabled_connector_is_visible_to_no_one(self):
        assert not connector_visible_to_user(
            _provider([], enabled=False), ["system_admin"]
        )

    def test_restricted_connector_needs_a_shared_role(self):
        provider = _provider(["system_admin"])
        assert connector_visible_to_user(provider, ["system_admin"])
        assert not connector_visible_to_user(provider, ["developer"])


class TestConnectorRoleIds:
    @pytest.mark.asyncio
    async def test_adds_an_enabled_parent(self):
        # The dev case: Entra's DotNetDevelopers maps to `developer`, which
        # inherits `system_admin` and so gets its tools. Its connectors must
        # admit the same user.
        roles = _Roles(
            {
                "developer": _role("developer", inherits_from=["system_admin"]),
                "system_admin": _role("system_admin"),
            }
        )
        role_ids = await connector_role_ids(_permissions(["developer"]), roles)

        assert role_ids == {"developer", "system_admin"}
        assert connector_visible_to_user(_provider(["system_admin"]), role_ids)

    @pytest.mark.asyncio
    async def test_skips_a_disabled_parent(self):
        # Mirrors `_compute_effective_permissions`: a disabled parent's tools
        # are not inherited, so neither are its connectors.
        roles = _Roles(
            {
                "developer": _role("developer", inherits_from=["system_admin"]),
                "system_admin": _role("system_admin", enabled=False),
            }
        )
        assert await connector_role_ids(_permissions(["developer"]), roles) == {
            "developer"
        }

    @pytest.mark.asyncio
    async def test_inheritance_is_one_level(self):
        roles = _Roles(
            {
                "intern": _role("intern", inherits_from=["developer"]),
                "developer": _role("developer", inherits_from=["system_admin"]),
                "system_admin": _role("system_admin"),
            }
        )
        assert await connector_role_ids(_permissions(["intern"]), roles) == {
            "intern",
            "developer",
        }

    @pytest.mark.asyncio
    async def test_a_failed_role_read_narrows_rather_than_widens(self):
        class _Broken:
            async def get_role(self, role_id: str) -> Optional[AppRole]:
                raise RuntimeError("cache unavailable")

        assert await connector_role_ids(_permissions(["developer"]), _Broken()) == {
            "developer"
        }

    @pytest.mark.asyncio
    async def test_app_roles_are_not_widened(self):
        # Admin checks read `app_roles` directly, and admin power does not
        # inherit. The connector rule builds its own set and leaves it alone.
        permissions = _permissions(["developer"])
        roles = _Roles(
            {
                "developer": _role("developer", inherits_from=["system_admin"]),
                "system_admin": _role("system_admin"),
            }
        )
        await connector_role_ids(permissions, roles)

        assert permissions.app_roles == ["developer"]
