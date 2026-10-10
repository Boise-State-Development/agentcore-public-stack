"""Slash-command skill invocation (`/skill-name` in the composer).

Two halves, both in inference-api chat routes:

* ``_resolve_invoked_skill_slugs`` — narrow-never-grant, the same rule
  ``_apply_enabled_skills_filter`` applies to ``enabled_skills``, re-run
  against the turn's FINAL effective set because an Agent's skill bindings can
  replace that set after the first filter runs.
* ``_build_skill_invocation_note`` — the directive appended to the user
  message. A slash command has to be expressed as an instruction because the
  only activation path is the plugin's own ``skills`` tool, which the model
  calls.
"""

from apis.inference_api.chat.routes import (
    _build_skill_invocation_note,
    _resolve_invoked_skill_slugs,
)


class TestResolveInvokedSkillSlugs:
    def test_returns_activation_slugs_not_catalog_ids(self):
        # The id never appears in anything the model can see; the slug is what
        # the `skills` tool takes and what `<available_skills>` lists.
        assert _resolve_invoked_skill_slugs(["pdf_workflows"], ["pdf_workflows"]) == [
            "pdf-workflows"
        ]

    def test_a_pinned_skill_matches_the_id_the_client_names(self):
        # A project's pinned skill runs as ``pdf_workflows@2``; the composer names
        # the catalog id, and the slug the model sees has no version in it.
        assert _resolve_invoked_skill_slugs(["pdf_workflows@2"], ["pdf_workflows"]) == [
            "pdf-workflows"
        ]

    def test_drops_a_skill_the_turn_does_not_disclose(self):
        # Narrow, never grant. A directive naming a skill absent from
        # <available_skills> would cost the model a tool call to discover.
        assert _resolve_invoked_skill_slugs(["web_research"], ["pdf_workflows"]) == []

    def test_orders_by_the_effective_set_not_the_request(self):
        # The directive is persisted in the message, so two turns naming the
        # same skills must produce byte-identical text regardless of the order
        # the client happened to send them in.
        effective = ["alpha", "beta", "gamma"]
        assert _resolve_invoked_skill_slugs(effective, ["gamma", "alpha"]) == [
            "alpha",
            "gamma",
        ]

    def test_no_skills_on_the_turn_means_no_invocation(self):
        # An Agent binding that replaced the skill set with nothing, or a turn
        # that never asked for skills at all.
        assert _resolve_invoked_skill_slugs(None, ["web_research"]) == []
        assert _resolve_invoked_skill_slugs([], ["web_research"]) == []

    def test_absent_selection_is_inert(self):
        assert _resolve_invoked_skill_slugs(["web_research"], None) == []
        assert _resolve_invoked_skill_slugs(["web_research"], []) == []


class TestBuildSkillInvocationNote:
    def test_names_the_slug_and_the_activation_tool(self):
        note = _build_skill_invocation_note(["pdf-workflows"])
        assert "`pdf-workflows`" in note
        assert "`skills`" in note
        assert "skill with a slash command" in note

    def test_pluralizes_for_more_than_one_skill(self):
        note = _build_skill_invocation_note(["pdf-workflows", "web-research"])
        assert "skills with a slash command" in note
        assert "Activate each" in note

    def test_is_one_bounded_line(self):
        # It rides the user message: paid as input this turn and as cached
        # history on every later turn of the session. Keep it small.
        note = _build_skill_invocation_note(["pdf-workflows"])
        assert "\n" not in note
        assert len(note) < 250
