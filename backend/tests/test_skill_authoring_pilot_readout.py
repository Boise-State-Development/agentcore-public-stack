"""The skill-authoring pilot readout (``scripts/skill_authoring_pilot_readout.py``).

The pure parts, plus a drift guard: the script parses log lines the tools and
the approval hook write, so a reworded line must fail here rather than quietly
read as "nothing happened" in a pilot readout.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import skill_authoring_pilot_readout as readout  # noqa: E402

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


class TestLogShapes:
    def test_the_tools_success_line_parses(self, caplog):
        from agents.builtin_tools.skill_authoring.tools import _log_applied

        with caplog.at_level(logging.INFO):
            for tool in readout.WRITE_TOOLS:
                _log_applied(tool, "grant_review", "sess-1")
        _, applied, created = readout.parse_log_lines(r.getMessage() for r in caplog.records)
        assert applied == {tool: 1 for tool in readout.WRITE_TOOLS}
        assert created == ["grant_review"]

    def test_the_approval_hooks_pause_line_parses(self, caplog):
        import asyncio

        from agents.main_agent.session.hooks.tool_approval import MCPExternalApprovalHook

        hook = MCPExternalApprovalHook(approval_names_lookup=lambda _: {"my_skill_update"})
        event = MagicMock()
        event.tool_use = {"name": "my_skill_update", "toolUseId": "tu-9", "input": {}}
        event.interrupt = MagicMock(return_value="approved")
        with caplog.at_level(logging.INFO):
            asyncio.run(hook._gate(event))
        asked, _, _ = readout.parse_log_lines(r.getMessage() for r in caplog.records)
        assert asked == {"my_skill_update": 1}

    def test_the_older_create_line_still_counts(self):
        """Lines written before the shared format said 'created'."""
        _, applied, created = readout.parse_log_lines(["my_skill_create created zz_test"])
        assert applied == {"my_skill_create": 1} and created == ["zz_test"]


class TestParse:
    def test_a_resumed_pause_counts_once(self):
        lines = [
            "Pausing for user approval: tool=my_skill_update tool_use_id=tu-1",
            "Pausing for user approval: tool=my_skill_update tool_use_id=tu-1",  # the resume
            "Pausing for user approval: tool=my_skill_update tool_use_id=tu-2",
            "my_skill_update applied grant_review",
        ]
        asked, applied, created = readout.parse_log_lines(lines)
        assert asked == {"my_skill_update": 2}
        assert applied == {"my_skill_update": 1}
        assert created == []


class TestRolesAndOptIn:
    def test_roles_granting_counts_wildcards_and_skips_disabled_roles(self):
        roles = [
            {"PK": "ROLE#faculty", "roleId": "faculty", "effectivePermissions": {"tools": ["my_skill_create"], "skills": []}},
            {"PK": "ROLE#default", "roleId": "default", "effectivePermissions": {"tools": ["*"], "skills": []}},
            {"PK": "ROLE#old", "roleId": "old", "enabled": False, "effectivePermissions": {"tools": ["*"]}},
        ]
        assert readout.roles_granting(roles, "tools", "my_skill_create") == ["default", "faculty"]
        assert readout.roles_granting(roles, "skills", "skill_creator") == []

    def test_only_an_explicit_true_counts_as_opted_in(self):
        prefs = [
            {"toolPreferences": {"my_skill_create": True}},
            {"toolPreferences": {"my_skill_create": False}},
            {"toolPreferences": {}},
        ]
        assert readout.count_opted_in(prefs, "toolPreferences", "my_skill_create") == 1


class TestClassify:
    def _row(self, created, updated, files=0):
        return {"createdAt": created.isoformat(), "updatedAt": updated.isoformat(), "resources": [{}] * files}

    def test_a_deleted_skill(self):
        assert readout.classify_created("s", None, None, NOW, 7) == readout.CreatedSkill("s", exists=False)

    def test_kept_turned_on_and_edited(self):
        created = NOW - timedelta(days=8)
        result = readout.classify_created(
            "s", self._row(created, created + timedelta(days=2), files=2),
            {"skillPreferences": {"s": True}}, NOW, 7,
        )
        assert (result.kept, result.owner_turned_on, result.edited_after_create, result.files) == (True, True, True, 2)

    def test_a_same_minute_file_write_is_not_an_edit(self):
        created = NOW - timedelta(days=1)
        result = readout.classify_created("s", self._row(created, created + timedelta(seconds=20)), None, NOW, 7)
        assert result.edited_after_create is False and result.kept is False

    def test_render_reads_as_a_table(self):
        r = readout.Readout(
            since="2026-10-12",
            roles_granting_tool=["faculty"],
            roles_granting_skill=["faculty"],
            users_tool_on=1,
            users_skill_on=1,
            approvals_asked={"my_skill_update": 3},
            writes_applied={"my_skill_update": 2},
            writes_not_applied={"my_skill_update": 1},
            created=[readout.CreatedSkill("s", exists=True, files=1, age_days=8.0, kept=True)],
        )
        text = readout.render(r)
        assert "my_skill_update               3        2            1" in text
        assert "Skills created through the tools: 1 (kept: 1)" in text
