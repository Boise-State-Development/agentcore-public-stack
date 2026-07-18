"""SkillCatalogService tests (moto-backed).

Covers CRUD, allowedAppRoles hydration, and bidirectional role sync (writing
granted_skills onto AppRoles).
"""

import pytest

from apis.shared.rbac.models import AppRoleCreate
from apis.shared.skills.models import SkillDefinition, SkillStatus


def _skill(skill_id="pdf_workflows", **kw) -> SkillDefinition:
    defaults = dict(
        skill_id=skill_id,
        display_name="PDF Workflows",
        description="Fill, merge and split PDFs.",
        instructions="# PDF Workflows",
    )
    defaults.update(kw)
    return SkillDefinition(**defaults)


async def _seed_role(skill_service, role_id, admin_user, **kw):
    return await skill_service.app_role_admin_service.create_role(
        AppRoleCreate(role_id=role_id, display_name=role_id, **kw), admin_user
    )


# ── CRUD ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get(skill_service, admin_user):
    created = await skill_service.create_skill(_skill(), admin_user)
    assert created.created_by == "admin-1"

    fetched = await skill_service.get_skill("pdf_workflows")
    assert fetched is not None
    assert fetched.display_name == "PDF Workflows"


@pytest.mark.asyncio
async def test_get_all_skills_status_filter(skill_service, admin_user):
    await skill_service.create_skill(_skill("skill_one", status=SkillStatus.ACTIVE), admin_user)
    await skill_service.create_skill(_skill("skill_two", status=SkillStatus.DRAFT), admin_user)

    active = await skill_service.get_all_skills(status="active")
    assert [s.skill_id for s in active] == ["skill_one"]


@pytest.mark.asyncio
async def test_update_skill(skill_service, admin_user):
    await skill_service.create_skill(_skill(), admin_user)
    updated = await skill_service.update_skill(
        "pdf_workflows", {"display_name": "PDF Tools"}, admin_user
    )
    assert updated.display_name == "PDF Tools"
    assert updated.updated_by == "admin-1"


@pytest.mark.asyncio
async def test_update_missing_returns_none(skill_service, admin_user):
    assert await skill_service.update_skill("nope", {"display_name": "x"}, admin_user) is None


@pytest.mark.asyncio
async def test_soft_delete_disables(skill_service, admin_user):
    await skill_service.create_skill(_skill(), admin_user)
    assert await skill_service.delete_skill("pdf_workflows", admin_user, soft=True) is True

    reloaded = await skill_service.get_skill("pdf_workflows")
    assert reloaded.status == "disabled"


@pytest.mark.asyncio
async def test_hard_delete_removes_row(skill_service, admin_user):
    await skill_service.create_skill(_skill(), admin_user)
    assert await skill_service.delete_skill("pdf_workflows", admin_user, soft=False) is True
    assert await skill_service.get_skill("pdf_workflows") is None


@pytest.mark.asyncio
async def test_delete_missing_returns_false(skill_service, admin_user):
    assert await skill_service.delete_skill("nope", admin_user) is False


# ── role sync + hydration ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_roles_for_skill_bidirectional(skill_service, admin_user):
    await skill_service.create_skill(_skill(), admin_user)
    await _seed_role(skill_service, "editor", admin_user)
    await _seed_role(skill_service, "viewer", admin_user)

    # Grant to editor + viewer.
    await skill_service.set_roles_for_skill(
        "pdf_workflows", ["editor", "viewer"], admin_user
    )
    roles = {r.role_id for r in await skill_service.get_roles_for_skill("pdf_workflows")}
    assert roles == {"editor", "viewer"}

    editor = await skill_service.app_role_admin_service.get_role("editor")
    assert "pdf_workflows" in editor.granted_skills
    assert "pdf_workflows" in editor.effective_permissions.skills

    # Replace with just editor → viewer loses the grant.
    await skill_service.set_roles_for_skill("pdf_workflows", ["editor"], admin_user)
    roles = {r.role_id for r in await skill_service.get_roles_for_skill("pdf_workflows")}
    assert roles == {"editor"}
    viewer = await skill_service.app_role_admin_service.get_role("viewer")
    assert "pdf_workflows" not in viewer.granted_skills


@pytest.mark.asyncio
async def test_add_and_remove_roles(skill_service, admin_user):
    await skill_service.create_skill(_skill(), admin_user)
    await _seed_role(skill_service, "editor", admin_user)

    await skill_service.add_roles_to_skill("pdf_workflows", ["editor"], admin_user)
    assert {r.role_id for r in await skill_service.get_roles_for_skill("pdf_workflows")} == {"editor"}

    await skill_service.remove_roles_from_skill("pdf_workflows", ["editor"], admin_user)
    assert await skill_service.get_roles_for_skill("pdf_workflows") == []


@pytest.mark.asyncio
async def test_get_all_skills_hydrates_allowed_roles(skill_service, admin_user):
    await skill_service.create_skill(_skill(), admin_user)
    await _seed_role(skill_service, "editor", admin_user)
    await skill_service.set_roles_for_skill("pdf_workflows", ["editor"], admin_user)

    skills = await skill_service.get_all_skills(include_roles=True)
    target = next(s for s in skills if s.skill_id == "pdf_workflows")
    assert target.allowed_app_roles == ["editor"]


@pytest.mark.asyncio
async def test_reverse_lookup_returns_direct_granter_only(skill_service, admin_user):
    """The skill→roles reverse lookup indexes only *direct* grants (it reuses
    the tool GSI, which writes an item per granted skill). A child that merely
    inherits the skill resolves it in its effective permissions but does not
    appear as a granting role — same behavior as tools."""
    await skill_service.create_skill(_skill(), admin_user)
    await _seed_role(skill_service, "parent", admin_user, grantedSkills=["pdf_workflows"])
    await _seed_role(skill_service, "child", admin_user, inheritsFrom=["parent"])

    roles = {
        r.role_id: r for r in await skill_service.get_roles_for_skill("pdf_workflows")
    }
    assert set(roles) == {"parent"}
    assert roles["parent"].grant_type == "direct"

    # The child still resolves the skill via inheritance, even though it isn't
    # surfaced by the reverse lookup.
    child = await skill_service.app_role_admin_service.get_role("child")
    assert "pdf_workflows" in child.effective_permissions.skills
