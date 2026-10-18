"""An Agent's skills at the invocation seam, by turn shape.

`_apply_agent_skills_override` is where the resolver's skill set meets the
turn's `(agent_type, accessible_skill_ids)`. The viewer's own picks are
`viewer-skill`; whether they survive is what separates the shapes. An Agent
that binds no skills runs with none: an author who chose no skills must not
get an Agent whose skills change with whoever is chatting.
"""

from apis.inference_api.chat import service
from apis.inference_api.chat.agent_binding_resolver import ResolvedSkills
from apis.inference_api.chat.routes import (
    _apply_agent_skills_override,
    _resolve_invoked_skill_slugs,
)

VIEWER = ["viewer-skill"]


class TestEffectiveSkillsByTurnShape:
    def test_a_plain_chat_turn_keeps_the_viewers_skills(self):
        assert _apply_agent_skills_override("chat", VIEWER, None) == ("chat", VIEWER)

    def test_a_plain_chat_turn_with_no_skills_is_untouched(self):
        assert _apply_agent_skills_override("chat", None, None) == ("chat", None)

    def test_an_unbound_agent_ignores_the_viewers_skills(self):
        assert _apply_agent_skills_override("chat", VIEWER, ResolvedSkills(skill_ids=[])) == (
            "skill",
            [],
        )

    def test_a_bound_agent_runs_its_own_skills(self):
        bound = ResolvedSkills(skill_ids=["rubric-writer"])
        assert _apply_agent_skills_override("chat", VIEWER, bound) == ("skill", ["rubric-writer"])

    def test_a_project_harness_with_no_skills_runs_with_none(self):
        # The harness already resolved to an empty set before this change; the seam is
        # the same one an unbound Agent now takes.
        assert _apply_agent_skills_override("skill", VIEWER, ResolvedSkills(skill_ids=[])) == (
            "skill",
            [],
        )

    def test_a_slash_command_on_an_unbound_agent_activates_nothing(self):
        # The viewer can still type `/viewer-skill`; the turn discloses no skills, so
        # the directive is dropped rather than naming a skill the model cannot see.
        _, effective = _apply_agent_skills_override("chat", VIEWER, ResolvedSkills(skill_ids=[]))
        assert _resolve_invoked_skill_slugs(effective, VIEWER) == []


class TestCacheSlot:
    def test_two_viewers_of_an_unbound_agent_share_one_cache_slot(self):
        """`agent_type` and `skills_hash` are key elements, and the hash is "" for an
        empty set. A viewer's picks reaching an unbound Agent's turn would key its
        cached agent by viewer, and could serve another viewer's skills from cache."""

        def key(agent_type, skill_ids):
            return service._create_cache_key(
                session_id="s1", user_id="u1", enabled_tools=[], model_id="m",
                inference_params={}, system_prompt=None, caching_enabled=False,
                provider="bedrock", freshness_hash="f", agent_type=agent_type,
                skills_hash="h" if skill_ids else "", assistant_id="ast-1",
            )

        unbound = ResolvedSkills(skill_ids=[])
        with_picks = _apply_agent_skills_override("chat", VIEWER, unbound)
        without = _apply_agent_skills_override("chat", None, unbound)
        assert with_picks == without
        assert key(*with_picks) == key(*without)
