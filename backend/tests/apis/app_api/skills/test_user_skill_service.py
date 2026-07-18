"""UserSkillService — the owner-scoped user-authored tier (Skills v2 PR-3).

Covers the two things that make this tier safe: ids are allocated server-side
without ever colliding across tiers, and ownership is checked on every path.
"""

import pytest

from apis.app_api.skills.user_service import (
    MAX_SKILLS_PER_USER,
    UserSkillError,
    UserSkillLimitError,
    UserSkillNotFoundError,
    slugify_skill_id,
)
from apis.shared.skills.models import (
    SYSTEM_OWNER_ID,
    SkillDefinition,
    SkillStatus,
    SkillVisibility,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# slugify_skill_id
# =============================================================================


@pytest.mark.parametrize(
    "display_name,expected",
    [
        ("Docx", "docx"),
        ("PDF Workflows", "pdf_workflows"),
        ("  Grant   Writing!  ", "grant_writing"),
        ("Weekly-Report/Builder", "weekly_report_builder"),
        # Pattern demands a leading letter, so a digit-led name gets a prefix
        # rather than a truncation that would change its meaning.
        ("3D Modeling", "skill_3d_modeling"),
        # Non-Latin script slugifies to nothing usable → the fallback stem.
        ("日本語", "skill"),
        ("", "skill"),
        # Minimum length is 3.
        ("Q", "q_x"),
    ],
)
def test_slugify_skill_id_shapes(display_name, expected):
    assert slugify_skill_id(display_name) == expected


def test_slugify_skill_id_always_matches_the_id_pattern():
    import re

    from apis.shared.skills.models import SKILL_ID_PATTERN

    names = ["Docx", "3D", "日本語", "", "Q", "A" * 200, "--- ??? ---"]
    for name in names:
        assert re.match(SKILL_ID_PATTERN, slugify_skill_id(name)), name


# =============================================================================
# create
# =============================================================================


async def test_create_allocates_id_and_stamps_ownership(
    user_skill_service, author_user
):
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Grant Writing",
        description="How we write grant narratives.",
        instructions="# Grant Writing\n\nStart with the abstract.",
    )

    assert skill.skill_id == "grant_writing"
    assert skill.owner_id == author_user.user_id
    assert skill.visibility == SkillVisibility.PRIVATE
    assert skill.status == SkillStatus.ACTIVE
    assert skill.created_by == author_user.user_id


async def test_create_suffixes_around_a_catalog_collision(
    user_skill_service, skill_service, admin_user, author_user
):
    """A user naming their skill after a catalog skill succeeds, suffixed.

    Rejecting with a 409 would also disclose the existence of a catalog skill
    the user may not be granted.
    """
    await skill_service.create_skill(
        SkillDefinition(
            skill_id="docx",
            display_name="Docx",
            description="Admin catalog docx skill.",
            instructions="body",
        ),
        admin_user,
    )

    mine = await user_skill_service.create_my_skill(
        author_user, display_name="Docx", description="My own docx notes."
    )

    assert mine.skill_id == "docx_2"
    assert mine.owner_id == author_user.user_id


async def test_create_suffixes_around_another_users_skill(
    user_skill_service, author_user, other_user
):
    first = await user_skill_service.create_my_skill(
        author_user, display_name="Docx", description="Mine."
    )
    second = await user_skill_service.create_my_skill(
        other_user, display_name="Docx", description="Theirs."
    )

    assert first.skill_id == "docx"
    assert second.skill_id == "docx_2"


async def test_create_requires_name_and_description(user_skill_service, author_user):
    with pytest.raises(UserSkillError):
        await user_skill_service.create_my_skill(
            author_user, display_name="   ", description="Has a description."
        )
    with pytest.raises(UserSkillError):
        await user_skill_service.create_my_skill(
            author_user, display_name="Named", description="  "
        )


async def test_create_enforces_the_per_user_cap(
    user_skill_service, author_user, monkeypatch
):
    monkeypatch.setattr(
        "apis.app_api.skills.user_service.MAX_SKILLS_PER_USER", 2, raising=False
    )
    # The service reads the module-level constant at call time.
    import apis.app_api.skills.user_service as mod

    monkeypatch.setattr(mod, "MAX_SKILLS_PER_USER", 2)

    await user_skill_service.create_my_skill(
        author_user, display_name="One", description="d"
    )
    await user_skill_service.create_my_skill(
        author_user, display_name="Two", description="d"
    )

    with pytest.raises(UserSkillLimitError):
        await user_skill_service.create_my_skill(
            author_user, display_name="Three", description="d"
        )

    assert MAX_SKILLS_PER_USER == 50  # the shipped default is unchanged


# =============================================================================
# ownership isolation
# =============================================================================


async def test_list_my_skills_returns_only_own(
    user_skill_service, skill_service, admin_user, author_user, other_user
):
    await user_skill_service.create_my_skill(
        author_user, display_name="Mine A", description="d"
    )
    await user_skill_service.create_my_skill(
        author_user, display_name="Mine B", description="d"
    )
    await user_skill_service.create_my_skill(
        other_user, display_name="Theirs", description="d"
    )
    await skill_service.create_skill(
        SkillDefinition(
            skill_id="catalog_one",
            display_name="Catalog",
            description="d",
            instructions="",
        ),
        admin_user,
    )

    mine = await user_skill_service.list_my_skills(author_user)

    assert [s.display_name for s in mine] == ["Mine A", "Mine B"]


async def test_another_users_skill_is_not_found_not_forbidden(
    user_skill_service, author_user, other_user
):
    """404, not 403 — this surface never confirms someone else's skill exists."""
    theirs = await user_skill_service.create_my_skill(
        other_user, display_name="Theirs", description="d"
    )

    with pytest.raises(UserSkillNotFoundError):
        await user_skill_service.get_my_skill(theirs.skill_id, author_user)
    with pytest.raises(UserSkillNotFoundError):
        await user_skill_service.update_my_skill(
            theirs.skill_id, {"description": "hijacked"}, author_user
        )
    with pytest.raises(UserSkillNotFoundError):
        await user_skill_service.delete_my_skill(theirs.skill_id, author_user)


async def test_catalog_skills_are_not_editable_through_the_user_tier(
    user_skill_service, skill_service, admin_user, author_user
):
    await skill_service.create_skill(
        SkillDefinition(
            skill_id="catalog_one",
            display_name="Catalog",
            description="d",
            instructions="",
        ),
        admin_user,
    )

    with pytest.raises(UserSkillNotFoundError):
        await user_skill_service.update_my_skill(
            "catalog_one", {"instructions": "hijacked"}, author_user
        )


# =============================================================================
# update / delete
# =============================================================================


async def test_update_writes_authored_fields(user_skill_service, author_user):
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Notes", description="Old."
    )

    updated = await user_skill_service.update_my_skill(
        skill.skill_id,
        {"description": "New.", "instructions": "# Notes", "allowed_tools": ["web"]},
        author_user,
    )

    assert updated.description == "New."
    assert updated.instructions == "# Notes"
    assert updated.allowed_tools == ["web"]
    # Ownership is untouched by an update.
    assert updated.owner_id == author_user.user_id
    assert updated.visibility == SkillVisibility.PRIVATE


async def test_delete_is_hard_and_purges_bundle_objects(
    user_skill_service, skill_resource_store, author_user
):
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Notes", description="d"
    )
    refs = await user_skill_service.add_resource(
        skill.skill_id, "forms.md", b"# Forms", "text/markdown", author_user
    )
    s3_key = refs[0].s3_key
    assert skill_resource_store.get(s3_key) == b"# Forms"

    await user_skill_service.delete_my_skill(skill.skill_id, author_user)

    assert await user_skill_service.repository.get_skill(skill.skill_id) is None
    from apis.shared.skills.resource_store import SkillResourceStoreError

    with pytest.raises(SkillResourceStoreError):
        skill_resource_store.get(s3_key)


# =============================================================================
# bundle files
# =============================================================================


async def test_resources_land_in_the_standard_bundle_layout(
    user_skill_service, author_user
):
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Notes", description="d"
    )

    await user_skill_service.add_resource(
        skill.skill_id, "forms.md", b"# Forms", "text/markdown", author_user
    )
    refs = await user_skill_service.add_resource(
        skill.skill_id, "build.py", b"print(1)", "text/x-python", author_user,
        kind="script",
    )

    by_name = {r.filename: r for r in refs}
    assert by_name["forms.md"].s3_key == f"skills/{skill.skill_id}/references/forms.md"
    assert by_name["build.py"].s3_key == f"skills/{skill.skill_id}/scripts/build.py"
    assert by_name["build.py"].kind == "script"


async def test_resource_routes_enforce_ownership(
    user_skill_service, author_user, other_user
):
    skill = await user_skill_service.create_my_skill(
        author_user, display_name="Notes", description="d"
    )
    await user_skill_service.add_resource(
        skill.skill_id, "forms.md", b"# Forms", "text/markdown", author_user
    )

    for call in (
        user_skill_service.list_resources(skill.skill_id, other_user),
        user_skill_service.read_resource(skill.skill_id, "forms.md", other_user),
        user_skill_service.delete_resource(skill.skill_id, "forms.md", other_user),
    ):
        with pytest.raises(UserSkillNotFoundError):
            await call


async def test_skill_md_projection_is_written_for_user_skills(
    user_skill_service, skill_resource_store, author_user
):
    """A user skill's S3 prefix must be a valid, portable agentskills.io bundle."""
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Grant Writing",
        description="How we write grant narratives.",
        instructions="Start with the abstract.",
    )

    content = skill_resource_store.get(f"skills/{skill.skill_id}/SKILL.md").decode()

    assert content.startswith("---")
    assert "How we write grant narratives." in content
    assert "Start with the abstract." in content


# =============================================================================
# tier boundaries
# =============================================================================


async def test_admin_catalog_list_excludes_user_skills(
    user_skill_service, skill_service, admin_user, author_user
):
    await skill_service.create_skill(
        SkillDefinition(
            skill_id="catalog_one",
            display_name="Catalog",
            description="d",
            instructions="",
        ),
        admin_user,
    )
    await user_skill_service.create_my_skill(
        author_user, display_name="Mine", description="d"
    )

    catalog = await skill_service.get_all_skills(include_roles=False)

    assert [s.skill_id for s in catalog] == ["catalog_one"]
    assert all(s.owner_id == SYSTEM_OWNER_ID for s in catalog)


async def test_user_skills_cannot_be_granted_to_app_roles(
    user_skill_service, skill_service, admin_user, author_user
):
    """Granting a private user skill to a role would leak it to that role."""
    mine = await user_skill_service.create_my_skill(
        author_user, display_name="Mine", description="d"
    )

    with pytest.raises(ValueError, match="user-authored"):
        await skill_service.set_roles_for_skill(mine.skill_id, ["some_role"], admin_user)
    with pytest.raises(ValueError, match="user-authored"):
        await skill_service.add_roles_to_skill(mine.skill_id, ["some_role"], admin_user)
