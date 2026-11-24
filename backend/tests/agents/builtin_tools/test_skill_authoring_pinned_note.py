"""The ``my_skill_*`` tools' pinned-behind note (skill-version-history.md D9).

One bounded line, a count and no names, only while history is on and only when a
pinned place is behind; a lookup that fails says nothing rather than failing the
write that already succeeded.
"""

from __future__ import annotations

from types import SimpleNamespace

from agents.builtin_tools.skill_authoring import tools as skill_tools
from apis.shared.skills import uses as uses_module
from apis.shared.skills.reach import SkillBinding
from apis.shared.skills.uses import SkillUses


class _Service:
    async def get_my_skill(self, skill_id, user):
        return SimpleNamespace(skill_id=skill_id)


def _patch(monkeypatch, result):
    monkeypatch.setattr(skill_tools, "get_user_skill_service", lambda: _Service())

    async def classify(live, owner_id):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(uses_module, "classify_uses", classify)


def test_off_says_nothing(monkeypatch):
    monkeypatch.delenv("SKILL_VERSION_HISTORY_ENABLED", raising=False)
    _patch(monkeypatch, SkillUses(behind=[SkillBinding("Lab", True, True, 2)]))
    assert skill_tools._pinned_behind_note("grant_review", "u1", None) == ""


def test_on_counts_what_is_behind_without_names(monkeypatch):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
    _patch(monkeypatch, SkillUses(behind=[SkillBinding("Secret Lab", True, True, 2)], others_behind=1))
    note = skill_tools._pinned_behind_note("grant_review", "u1", None)
    assert note.startswith(" 2 agent or project places pin an earlier version")
    assert "Secret Lab" not in note


def test_on_with_nothing_behind_says_nothing(monkeypatch):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
    _patch(monkeypatch, SkillUses(current=[SkillBinding("Live", False, True, None)]))
    assert skill_tools._pinned_behind_note("grant_review", "u1", None) == ""


def test_a_failed_lookup_says_nothing(monkeypatch):
    monkeypatch.setenv("SKILL_VERSION_HISTORY_ENABLED", "true")
    _patch(monkeypatch, RuntimeError("dynamodb down"))
    assert skill_tools._pinned_behind_note("grant_review", "u1", None) == ""
