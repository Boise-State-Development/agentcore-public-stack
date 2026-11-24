"""Where a skill is used, split by whether each place runs its current content
(docs/specs/skill-version-history.md D9; ``apis.shared.skills.uses``)."""

from __future__ import annotations

import asyncio

from apis.shared.skills import uses as uses_module
from apis.shared.skills.models import SkillDefinition
from apis.shared.skills.reach import SkillBinding, SkillReach
from apis.shared.skills.uses import SkillUses, behind_note, classify_uses
from apis.shared.skills.versions import SkillVersion, content_hash

LIVE = SkillDefinition(skill_id="sk_one", display_name="S", description="d", instructions="now")
OLD = LIVE.model_copy(update={"instructions": "before"})


def _version(n: int, skill: SkillDefinition) -> SkillVersion:
    return SkillVersion(skill_id="sk_one", version=n, content_hash=content_hash(skill), display_name="S")


class _Repo:
    def __init__(self, versions):
        self.versions = {(v.skill_id, v.version): v for v in versions}
        self.asked = []

    async def batch_get_skill_versions(self, refs):
        self.asked.append(list(refs))
        return {r: self.versions[r] for r in refs if r in self.versions}


def _classify(monkeypatch, bindings, versions, **reach):
    monkeypatch.setattr(
        uses_module, "lookup_skill_reach", lambda skill_id, owner: SkillReach(skill_id, bindings, **reach)
    )
    repo = _Repo(versions)
    return asyncio.run(classify_uses(LIVE, "author", repository=repo)), repo


def test_splits_current_behind_and_others(monkeypatch):
    bindings = [
        SkillBinding("Live agent", False, True, None, agent_id="a1"),
        SkillBinding("Up to date", False, True, 3, agent_id="a2"),
        SkillBinding("Behind", True, True, 2, agent_id="h1", project_id="p1"),
        SkillBinding("Viewer now", True, True, 2, agent_id="h2", project_id="p2", can_update=False),
    ]
    uses, repo = _classify(monkeypatch, bindings, [_version(2, OLD), _version(3, LIVE)])
    assert [b.name for b in uses.current] == ["Live agent", "Up to date"]
    assert [b.name for b in uses.behind] == ["Behind"]
    assert uses.others_behind == 1
    assert uses.complete is True
    assert repo.asked == [[("sk_one", 2), ("sk_one", 3)]]


def test_a_pin_on_a_missing_version_is_behind(monkeypatch):
    uses, _ = _classify(monkeypatch, [SkillBinding("Gone", False, False, 9, agent_id="a1")], [])
    assert [b.name for b in uses.behind] == ["Gone"]


def test_nothing_pinned_reads_no_versions(monkeypatch):
    uses, repo = _classify(monkeypatch, [SkillBinding("Live", False, True, None)], [])
    assert repo.asked == []
    assert uses.behind_count == 0


def test_an_incomplete_lookup_says_so(monkeypatch):
    uses, _ = _classify(monkeypatch, [], [], complete=False)
    assert uses.complete is False
    uses, _ = _classify(monkeypatch, [], [], editor_projects_checked=False)
    assert uses.complete is False


def test_behind_note_is_a_count_only_when_something_is_behind():
    assert behind_note(SkillUses()) is None
    one = behind_note(SkillUses(behind=[SkillBinding("Secret Project", True, True, 2)]))
    assert one.startswith("1 agent or project place pins an earlier version")
    assert "Secret Project" not in one
    assert behind_note(SkillUses(others_behind=3)).startswith("3 agent or project places pin")
