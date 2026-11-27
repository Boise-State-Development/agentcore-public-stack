"""Memory tool specs for a Shared Project's harness.

The spec text is a constant in the cacheable ``toolConfig``, so its wording is pinned.
"""


class TestProjectHarnessSpecs:
    def _specs(self):
        from agents.builtin_tools.memory_spaces.project_tools import (
            ProjectMemoryScopes,
            make_project_memory_tools,
        )
        from apis.shared.auth.models import User

        user = User(email="member@example.edu", user_id="u1", name="M", roles=[])
        tools = make_project_memory_tools(ProjectMemoryScopes.for_member("prj_1", "spc_1", None, user))
        return {t.tool_spec["name"]: t.tool_spec for t in tools}

    def test_every_tool_takes_a_scope_enum(self):
        specs = self._specs()
        assert list(specs) == ["memory_list", "memory_read", "memory_query", "memory_save", "memory_propose"]
        # Proposals only ever target the project's shared memory (2.5a).
        assert "scope" not in specs.pop("memory_propose")["inputSchema"]["json"]["properties"]
        for spec in specs.values():
            schema = spec["inputSchema"]["json"]
            assert "scope" in schema["required"]
            scope = schema["properties"]["scope"]
            assert scope["enum"] == ["project", "mine"]

    def test_save_teaches_the_item_format(self):
        description = self._specs()["memory_save"]["description"]
        assert 'one fact per "- " line' in description
        assert "prose and headings are rejected" in description
        assert "editor" in description

    def test_save_says_when_to_use_mine_and_to_write_the_rule(self):
        """G17: asked to "remember this just for me", Haiku kept it in the conversation."""
        description = " ".join(self._specs()["memory_save"]["description"].split())
        assert 'save it to "mine" now' in description
        assert "will not carry it into their next one" in description
        assert "Make `description` the rule itself" in description
        # QA 2026-10-15: "I've saved your preference" with no tool call, mid-conversation.
        assert "never tell the member something is saved without calling it" in description

    def test_save_says_its_description_is_the_index_line_on_every_save(self):
        """The 2026-10-15 QA pass: an update left the index line naming the old value."""
        spec = self._specs()["memory_save"]
        description = " ".join(spec["description"].split())
        assert "`description` is the file's line in that scope's MEMORY.md index" in description
        assert "Send it on every save" in description
        assert "the index line is rewritten to match it" in description
        assert "`memory_read` it first" in description
        assert "description" in spec["inputSchema"]["json"]["required"]

    def test_save_says_an_unread_file_is_refused(self):
        """B6: a save is conditional on the version this conversation read."""
        description = " ".join(self._specs()["memory_save"]["description"].split())
        assert "a save to a file this conversation hasn't read is refused" in description

    def test_specs_are_the_same_for_every_member_and_project(self):
        from agents.builtin_tools.memory_spaces.project_tools import (
            ProjectMemoryScopes,
            make_project_memory_tools,
        )
        from apis.shared.auth.models import User

        other = User(email="viewer@example.edu", user_id="u2", name="V", roles=[])
        tools = make_project_memory_tools(ProjectMemoryScopes.for_member("prj_2", None, "spc_9", other))
        assert {t.tool_spec["name"]: t.tool_spec for t in tools} == self._specs()

    def test_save_says_it_writes_the_index_line_itself(self):
        """Dev: a file and MEMORY.md saved in one batch, one of them refused every time."""
        description = " ".join(self._specs()["memory_save"]["description"].split())
        assert "The save writes that line itself, so don't also save MEMORY.md to add or change a file's line" in description

    def test_save_and_propose_state_the_description_limit(self):
        """Production: a description past the limit was memory_save's most common refusal,
        and the spec asked for "the rule itself" without saying it is capped."""
        from apis.shared.memory.format import MAX_DESCRIPTION_CHARS

        specs = self._specs()
        for name in ("memory_save", "memory_propose"):
            arg = specs[name]["inputSchema"]["json"]["properties"]["description"]["description"]
            # A target well under the limit: told only the limit, a model aimed at it and overshot.
            assert f"about 100 characters and never over {MAX_DESCRIPTION_CHARS}" in " ".join(arg.split()), name
        assert MAX_DESCRIPTION_CHARS >= 150
        description = " ".join(specs["memory_save"]["description"].split())
        assert f"in about 100 characters (one over {MAX_DESCRIPTION_CHARS} is refused" in description

    #: The specs sit in the cacheable ``toolConfig``. A change here is a deliberate
    #: prefix rewrite at deploy: update the pin with it and state the token delta in
    #: the PR. Last changed when the description target became about 100 characters.
    SPEC_DIGEST = "8628517cbbc7362627d2d94821fe3b0cf258ea63082c276003afc5128a79ed82"

    def test_the_specs_are_pinned(self):
        import hashlib
        import json

        digest = hashlib.sha256(json.dumps(list(self._specs().values()), sort_keys=True).encode()).hexdigest()
        assert digest == self.SPEC_DIGEST
