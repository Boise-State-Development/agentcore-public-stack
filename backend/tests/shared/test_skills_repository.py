"""SkillCatalogRepository CRUD tests (moto DynamoDB).

Reuses the shared `roles_table` fixture (the app-roles table) via the
`skill_repository` fixture, mirroring test_rbac_repository.py.
"""

import pytest

from apis.shared.skills.models import SkillDefinition, SkillStatus


def _make_skill(skill_id="pdf_workflows", **kw) -> SkillDefinition:
    defaults = dict(
        skill_id=skill_id,
        display_name="PDF Workflows",
        description="Fill, merge and split PDFs.",
        instructions="# PDF Workflows\nUse the bound tools.",
        bound_tool_ids=["fill_pdf_form"],
    )
    defaults.update(kw)
    return SkillDefinition(**defaults)


class TestSkillCatalogRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self, skill_repository):
        created = await skill_repository.create_skill(_make_skill())
        assert created.skill_id == "pdf_workflows"

        result = await skill_repository.get_skill("pdf_workflows")
        assert result is not None
        assert result.display_name == "PDF Workflows"
        assert result.bound_tool_ids == ["fill_pdf_form"]

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, skill_repository):
        assert await skill_repository.get_skill("nope") is None

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, skill_repository):
        await skill_repository.create_skill(_make_skill())
        with pytest.raises(ValueError):
            await skill_repository.create_skill(_make_skill())

    @pytest.mark.asyncio
    async def test_list_skills(self, skill_repository):
        await skill_repository.create_skill(_make_skill("skill_one"))
        await skill_repository.create_skill(_make_skill("skill_two"))
        skills = await skill_repository.list_skills()
        assert {s.skill_id for s in skills} == {"skill_one", "skill_two"}

    @pytest.mark.asyncio
    async def test_list_does_not_pick_up_other_pk_items(
        self, skill_repository, role_repository
    ):
        # A non-skill item in the same table must not appear in list_skills.
        from apis.shared.rbac.models import AppRole

        await role_repository.create_role(
            AppRole(role_id="r1", display_name="R1", description="x")
        )
        await skill_repository.create_skill(_make_skill("skill_one"))

        skills = await skill_repository.list_skills()
        assert [s.skill_id for s in skills] == ["skill_one"]

    @pytest.mark.asyncio
    async def test_list_status_filter(self, skill_repository):
        await skill_repository.create_skill(_make_skill("skill_one", status=SkillStatus.ACTIVE))
        await skill_repository.create_skill(_make_skill("skill_two", status=SkillStatus.DRAFT))

        active = await skill_repository.list_skills(status="active")
        assert [s.skill_id for s in active] == ["skill_one"]

        draft = await skill_repository.list_skills(status="draft")
        assert [s.skill_id for s in draft] == ["skill_two"]

    @pytest.mark.asyncio
    async def test_update_skill(self, skill_repository):
        await skill_repository.create_skill(_make_skill())
        updated = await skill_repository.update_skill(
            "pdf_workflows",
            {"display_name": "PDF Tools", "bound_tool_ids": ["fill_pdf_form", "merge_pdf"]},
            admin_user_id="admin-2",
        )
        assert updated is not None
        assert updated.display_name == "PDF Tools"
        assert updated.bound_tool_ids == ["fill_pdf_form", "merge_pdf"]
        assert updated.updated_by == "admin-2"

        # Persisted.
        reloaded = await skill_repository.get_skill("pdf_workflows")
        assert reloaded.display_name == "PDF Tools"
        assert reloaded.bound_tool_ids == ["fill_pdf_form", "merge_pdf"]

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_none(self, skill_repository):
        assert await skill_repository.update_skill("nope", {"display_name": "x"}) is None

    @pytest.mark.asyncio
    async def test_soft_delete_sets_disabled(self, skill_repository):
        await skill_repository.create_skill(_make_skill())
        result = await skill_repository.soft_delete_skill(
            "pdf_workflows", admin_user_id="admin-3"
        )
        assert result is not None
        assert result.status == "disabled"

        reloaded = await skill_repository.get_skill("pdf_workflows")
        assert reloaded.status == "disabled"

    @pytest.mark.asyncio
    async def test_skill_exists(self, skill_repository):
        await skill_repository.create_skill(_make_skill())
        assert await skill_repository.skill_exists("pdf_workflows") is True
        assert await skill_repository.skill_exists("nope") is False

    @pytest.mark.asyncio
    async def test_batch_get_skills(self, skill_repository):
        await skill_repository.create_skill(_make_skill("skill_one"))
        await skill_repository.create_skill(_make_skill("skill_two"))

        skills = await skill_repository.batch_get_skills(["skill_one", "skill_two", "ghost"])
        assert {s.skill_id for s in skills} == {"skill_one", "skill_two"}

    @pytest.mark.asyncio
    async def test_batch_get_empty(self, skill_repository):
        assert await skill_repository.batch_get_skills([]) == []
