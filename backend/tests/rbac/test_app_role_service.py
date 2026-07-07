"""Unit tests for AppRoleService permission resolution.

Covers:
- Tools union merge (5.2)
- Models union merge (5.3)
- Quota tier from highest priority (5.4)
- No matching roles falls back to default (5.5)
- No matching roles and no default returns empty (5.6)
- Wildcard in tools (5.7)
- can_access_tool with wildcard (5.8)
- can_access_tool with matching tool (5.9)
- can_access_tool with no match (5.10)
- Caching on second call (5.11)
- Cache miss queries repo (5.12)
- Only enabled roles merged (5.13)

Validates: Requirements 5.1–5.13
"""

import pytest

from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import AppRoleService


@pytest.fixture
def user():
    """A test user with two JWT roles."""
    return User(
        email="test@example.com",
        user_id="user-1",
        name="Test User",
        roles=["Editor", "Viewer"],
    )


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache):
    """AppRoleService wired to mock repo and cache."""
    return AppRoleService(repository=mock_app_role_repo, cache=mock_app_role_cache)


# ---------------------------------------------------------------------------
# 5.2 — Tools union merge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_union_merge(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """Merging multiple AppRoles produces the union of all tools."""
    role_a = make_app_role(role_id="editor", tools=["tool_a", "tool_b"], priority=1)
    role_b = make_app_role(role_id="viewer", tools=["tool_b", "tool_c"], priority=0)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": ["viewer"],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
        "viewer": role_b,
    }.get(rid)

    perms = await service.resolve_user_permissions(user)

    assert set(perms.tools) == {"tool_a", "tool_b", "tool_c"}


# ---------------------------------------------------------------------------
# 5.3 — Models union merge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_models_union_merge(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """Merging multiple AppRoles produces the union of all models."""
    role_a = make_app_role(role_id="editor", models=["model_x"], priority=1)
    role_b = make_app_role(role_id="viewer", models=["model_x", "model_y"], priority=0)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": ["viewer"],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
        "viewer": role_b,
    }.get(rid)

    perms = await service.resolve_user_permissions(user)

    assert set(perms.models) == {"model_x", "model_y"}


# ---------------------------------------------------------------------------
# 5.4 — Quota tier from highest priority
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quota_tier_from_highest_priority(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """Quota tier is taken from the role with the highest priority."""
    role_a = make_app_role(role_id="editor", quota_tier="basic", priority=5)
    role_b = make_app_role(role_id="viewer", quota_tier="pro", priority=10)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": ["viewer"],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
        "viewer": role_b,
    }.get(rid)

    perms = await service.resolve_user_permissions(user)

    assert perms.quota_tier == "pro"


# ---------------------------------------------------------------------------
# 5.5 — No matching roles falls back to default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_matching_roles_falls_back_to_default(service, mock_app_role_repo, mock_app_role_cache, make_app_role):
    """When no JWT roles match, the service falls back to the 'default' role."""
    user_no_match = User(
        email="nobody@example.com",
        user_id="user-2",
        name="Nobody",
        roles=["UnknownRole"],
    )
    default_role = make_app_role(role_id="default", tools=["basic_tool"], quota_tier="free", priority=0)

    mock_app_role_repo.get_roles_for_jwt_role.return_value = []
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "default": default_role,
    }.get(rid)

    perms = await service.resolve_user_permissions(user_no_match)

    assert perms.app_roles == ["default"]
    assert "basic_tool" in perms.tools


# ---------------------------------------------------------------------------
# 5.6 — No matching roles and no default returns empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_matching_roles_no_default_returns_empty(service, mock_app_role_repo, mock_app_role_cache):
    """When no JWT roles match and no default role exists, returns empty permissions."""
    user_no_match = User(
        email="nobody@example.com",
        user_id="user-3",
        name="Nobody",
        roles=["UnknownRole"],
    )

    mock_app_role_repo.get_roles_for_jwt_role.return_value = []
    mock_app_role_repo.get_role.return_value = None

    perms = await service.resolve_user_permissions(user_no_match)

    assert perms.app_roles == []
    assert perms.tools == []
    assert perms.models == []
    assert perms.quota_tier is None


# ---------------------------------------------------------------------------
# 5.7 — Wildcard in tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wildcard_in_tools(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """When any role has '*' in tools, the merged tools contain '*'."""
    role_a = make_app_role(role_id="editor", tools=["*"], priority=1)
    role_b = make_app_role(role_id="viewer", tools=["tool_c"], priority=0)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": ["viewer"],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
        "viewer": role_b,
    }.get(rid)

    perms = await service.resolve_user_permissions(user)

    assert "*" in perms.tools


# ---------------------------------------------------------------------------
# 5.8 — can_access_tool with wildcard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_can_access_tool_with_wildcard(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """can_access_tool returns True for any tool_id when permissions contain '*'."""
    role_a = make_app_role(role_id="editor", tools=["*"], priority=1)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
    }.get(rid)

    assert await service.can_access_tool(user, "any_tool") is True
    assert await service.can_access_tool(user, "another_tool") is True


# ---------------------------------------------------------------------------
# 5.9 — can_access_tool with matching tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_can_access_tool_with_matching_tool(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """can_access_tool returns True when the tool_id is in the user's tools list."""
    role_a = make_app_role(role_id="editor", tools=["tool_a", "tool_b"], priority=1)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
    }.get(rid)

    assert await service.can_access_tool(user, "tool_a") is True


# ---------------------------------------------------------------------------
# 5.10 — can_access_tool with no match
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_can_access_tool_with_no_match(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """can_access_tool returns False when the tool_id is not in the user's tools and no wildcard."""
    role_a = make_app_role(role_id="editor", tools=["tool_a"], priority=1)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
    }.get(rid)

    assert await service.can_access_tool(user, "tool_z") is False


# ---------------------------------------------------------------------------
# 5.11 — Caching on second call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caching_on_second_call(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """Second call returns cached result without querying the repository again."""
    role_a = make_app_role(role_id="editor", tools=["tool_a"], priority=1)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
    }.get(rid)

    # First call — cache miss, resolves from repo
    first_result = await service.resolve_user_permissions(user)

    # Configure cache to return the first result on second call
    mock_app_role_cache.get_user_permissions.return_value = first_result

    # Second call — should hit cache
    second_result = await service.resolve_user_permissions(user)

    assert second_result is first_result
    # set_user_permissions should only have been called once (first call)
    assert mock_app_role_cache.set_user_permissions.call_count == 1


# ---------------------------------------------------------------------------
# 5.12 — Cache miss queries repo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_miss_queries_repo(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """When cache is empty, the service queries the repository for JWT mappings."""
    role_a = make_app_role(role_id="editor", tools=["tool_a"], priority=1)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
    }.get(rid)

    await service.resolve_user_permissions(user)

    # Repo was queried for JWT mappings (cache was empty)
    assert mock_app_role_repo.get_roles_for_jwt_role.call_count == 2  # Editor + Viewer
    # JWT mappings were cached
    assert mock_app_role_cache.set_jwt_mapping.call_count == 2


# ---------------------------------------------------------------------------
# 5.13 — Only enabled roles merged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_enabled_roles_merged(service, mock_app_role_repo, mock_app_role_cache, make_app_role, user):
    """Disabled roles are excluded from permission merging."""
    role_enabled = make_app_role(role_id="editor", tools=["tool_a"], priority=1, enabled=True)
    role_disabled = make_app_role(role_id="viewer", tools=["tool_secret"], priority=0, enabled=False)

    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": ["viewer"],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_enabled,
        "viewer": role_disabled,
    }.get(rid)

    perms = await service.resolve_user_permissions(user)

    assert "tool_a" in perms.tools
    assert "tool_secret" not in perms.tools
    assert "viewer" not in perms.app_roles


# ---------------------------------------------------------------------------
# filter_requested_tools — narrow-never-grant intersection of a client list
# ---------------------------------------------------------------------------

def _wire_single_role(mock_app_role_repo, role):
    """Point the mock repo at one role mapped from the user's 'Editor' JWT role."""
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: (
        [role.role_id] if r == "Editor" else []
    )
    mock_app_role_repo.get_role.side_effect = lambda rid: (
        role if rid == role.role_id else None
    )


@pytest.mark.asyncio
async def test_filter_requested_tools_drops_ungranted(
    service, mock_app_role_repo, make_app_role, user
):
    """A requested tool outside the user's grant is silently dropped."""
    _wire_single_role(mock_app_role_repo, make_app_role(role_id="editor", tools=["tool_a", "tool_b"]))

    result = await service.filter_requested_tools(user, ["tool_a", "tool_secret", "tool_b"])

    assert result == ["tool_a", "tool_b"]


@pytest.mark.asyncio
async def test_filter_requested_tools_preserves_caller_order(
    service, mock_app_role_repo, make_app_role, user
):
    """The caller's ordering is preserved, not the grant's."""
    _wire_single_role(mock_app_role_repo, make_app_role(role_id="editor", tools=["tool_a", "tool_b", "tool_c"]))

    result = await service.filter_requested_tools(user, ["tool_c", "tool_a"])

    assert result == ["tool_c", "tool_a"]


@pytest.mark.asyncio
async def test_filter_requested_tools_wildcard_passes_everything(
    service, mock_app_role_repo, make_app_role, user
):
    """A ``*`` grant admits any requested tool (including ones not enumerated)."""
    _wire_single_role(mock_app_role_repo, make_app_role(role_id="admin", tools=["*"]))

    requested = ["tool_a", "gateway_anything", "some_admin_tool"]
    result = await service.filter_requested_tools(user, requested)

    assert result == requested


@pytest.mark.asyncio
async def test_filter_requested_tools_scoped_id_allowed_by_base_grant(
    service, mock_app_role_repo, make_app_role, user
):
    """A scoped id (base::tool) passes when its base server id is granted."""
    _wire_single_role(mock_app_role_repo, make_app_role(role_id="editor", tools=["gateway_wikipedia"]))

    result = await service.filter_requested_tools(user, ["gateway_wikipedia::search"])

    assert result == ["gateway_wikipedia::search"]


@pytest.mark.asyncio
async def test_filter_requested_tools_empty_grant_drops_all(
    service, mock_app_role_repo, make_app_role, user
):
    """No grant means an empty result — never a passthrough."""
    _wire_single_role(mock_app_role_repo, make_app_role(role_id="editor", tools=[]))

    result = await service.filter_requested_tools(user, ["tool_a", "tool_b"])

    assert result == []
