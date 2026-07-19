"""Unit tests for the shared per-user skill access resolver."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.auth.models import User
from apis.shared.skills.access import (
    resolve_accessible_skill_ids,
    resolve_invocable_skill_ids,
)

pytestmark = pytest.mark.asyncio


def _user() -> User:
    return User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        roles=["default"],
        raw_token="tok",
    )


class TestResolveAccessibleSkillIds:
    async def test_plain_grants_pass_through(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(
            return_value=["web_research", "pdf_workflows"]
        )
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ):
            result = await resolve_accessible_skill_ids(_user())
        assert result == ["web_research", "pdf_workflows"]

    async def test_wildcard_expands_to_all_known_skills_sorted(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["*"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), patch(
            "apis.shared.skills.freshness.get_all_skill_ids",
            AsyncMock(return_value=frozenset({"zeta", "alpha"})),
        ):
            result = await resolve_accessible_skill_ids(_user())
        assert result == ["alpha", "zeta"]

    async def test_failure_degrades_to_no_skills(self):
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            side_effect=RuntimeError("rbac unavailable"),
        ):
            result = await resolve_accessible_skill_ids(_user())
        assert result == []


def _owned(*skill_ids):
    """Patch the owner-index query to return skills with these ids."""
    records = [MagicMock(skill_id=sid) for sid in skill_ids]
    repo = MagicMock()
    repo.list_skills_by_owner = AsyncMock(return_value=records)
    return patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    )


class TestOwnedSkillUnion:
    """Ownership is its own grant — a user always reaches skills they authored."""

    async def test_owned_skills_are_unioned_onto_catalog_grants(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["pdf_workflows"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), _owned("my_notes"):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["pdf_workflows", "my_notes"]

    async def test_owned_skills_reach_a_user_with_no_role_grants(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=[])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), _owned("my_notes"):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["my_notes"]

    async def test_a_skill_both_granted_and_owned_appears_once(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["my_notes"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), _owned("my_notes"):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["my_notes"]

    async def test_owner_lookup_failure_still_yields_catalog_grants(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["pdf_workflows"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            side_effect=RuntimeError("dynamo down"),
        ):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["pdf_workflows"]


AGENT_OWNER = "user-alice"


def _repo(*, owned=(), records=()):
    """Patch the repo for both halves of the predicate.

    ``owned`` feeds the GSI4 owner query (clause 2, the *invoker's* own skills);
    ``records`` are ``(skill_id, owner_id)`` pairs returned by the batch read
    that clause 3 uses to test owner-match.
    """
    repo = MagicMock()
    repo.list_skills_by_owner = AsyncMock(
        return_value=[MagicMock(skill_id=sid) for sid in owned]
    )
    repo.batch_get_skills = AsyncMock(
        return_value=[
            MagicMock(skill_id=sid, owner_id=oid) for sid, oid in records
        ]
    )
    return patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ), repo


def _roles(*granted):
    role_service = MagicMock()
    role_service.get_accessible_skills = AsyncMock(return_value=list(granted))
    return patch(
        "apis.shared.rbac.service.get_app_role_service",
        return_value=role_service,
    )


class TestInvokeThroughPredicate:
    """§6/D7 — a bound skill resolves via catalog grant ∪ ownership ∪ invoke-through.

    Clause 3 (invoke-through) is what makes sharing an Agent with custom skills
    useful at all: the invoker holds no grant on the author's skill, but the
    share is the grant boundary, exactly as it already is for an assistant's KB
    documents.
    """

    async def test_catalog_grant_resolves(self):
        repo_patch, _ = _repo()
        with _roles("pdf_workflows"), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["pdf_workflows"], AGENT_OWNER
            )
        assert "pdf_workflows" in allowed

    async def test_own_skill_resolves_without_any_role(self):
        # The author binding their OWN skill into their OWN agent. This was
        # broken before PR-4: the resolver gated on roles only, so an author
        # was blocked on their own invocation.
        repo_patch, _ = _repo(owned=["my_notes"])
        with _roles(), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["my_notes"], AGENT_OWNER
            )
        assert "my_notes" in allowed

    async def test_invoke_through_grants_the_agent_owners_skill(self):
        # Alice authored `alices_style`, bound it to her agent, shared with us.
        repo_patch, _ = _repo(records=[("alices_style", AGENT_OWNER)])
        with _roles(), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["alices_style"], AGENT_OWNER
            )
        assert "alices_style" in allowed

    async def test_chain_sharing_is_blocked_by_owner_match(self):
        # `carols_skill` was merely shared TO Alice. Binding it to Alice's agent
        # and sharing that agent must NOT launder it to a wider audience —
        # invoke-through extends the agent owner's OWN skills and nothing else.
        repo_patch, _ = _repo(records=[("carols_skill", "user-carol")])
        with _roles(), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["carols_skill"], AGENT_OWNER
            )
        assert "carols_skill" not in allowed

    async def test_a_system_owned_agent_gets_no_invoke_through(self):
        # Owner-match on "system" would hand the entire admin catalog to anyone
        # who could invoke a system-owned agent, bypassing RBAC entirely.
        repo_patch, repo = _repo(records=[("payroll_runbook", "system")])
        with _roles(), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["payroll_runbook"], "system"
            )
        assert "payroll_runbook" not in allowed
        repo.batch_get_skills.assert_not_awaited()

    async def test_wildcard_role_does_not_reach_a_private_authored_skill(self):
        # The old `can_access_skill` returned True for "*" against ANY id.
        # Routing through the shared resolver expands "*" over the CATALOG only,
        # so another user's private skill stays unreachable.
        repo_patch, _ = _repo(records=[("carols_private", "user-carol")])
        with _roles("*"), patch(
            "apis.shared.skills.freshness.get_all_skill_ids",
            AsyncMock(return_value=frozenset({"pdf_workflows"})),
        ), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["carols_private"], AGENT_OWNER
            )
        assert "carols_private" not in allowed

    async def test_no_batch_read_when_every_ref_already_resolves(self):
        # Clause 3 costs a batch read; it must not run when clauses 1+2 cover
        # the bindings, which is the common case.
        repo_patch, repo = _repo(owned=["my_notes"])
        with _roles("pdf_workflows"), repo_patch:
            allowed = await resolve_invocable_skill_ids(
                _user(), ["pdf_workflows", "my_notes"], AGENT_OWNER
            )
        assert {"pdf_workflows", "my_notes"} <= allowed
        repo.batch_get_skills.assert_not_awaited()

    async def test_batch_read_failure_degrades_to_not_granted(self):
        repo = MagicMock()
        repo.list_skills_by_owner = AsyncMock(return_value=[])
        repo.batch_get_skills = AsyncMock(side_effect=RuntimeError("dynamo down"))
        with _roles(), patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            return_value=repo,
        ):
            allowed = await resolve_invocable_skill_ids(
                _user(), ["alices_style"], AGENT_OWNER
            )
        assert "alices_style" not in allowed
