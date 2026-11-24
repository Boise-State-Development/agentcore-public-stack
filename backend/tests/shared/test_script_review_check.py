"""The review a save must carry before it binds someone else's scripts
(``apis.shared.skills.script_review_check``; skill-code-sandbox §8.5).

Against a moto AppRoles table, so a review ``script_reviews`` writes is the one
the check finds.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from apis.shared.assistants.models import AgentBinding
from apis.shared.auth.models import User
from apis.shared.skills import script_review_check as rc
from apis.shared.skills import script_reviews as sr
from apis.shared.skills import script_trust as st
from apis.shared.skills.models import SkillResourceRef

AGENT = "agent-1"
OWNER = User(email="owner@example.com", user_id="owner-1", name="Owner", roles=[])


def _ref(path: str, data: bytes, kind: str = "script") -> SkillResourceRef:
    return SkillResourceRef(
        filename=path,
        content_hash=hashlib.sha256(data).hexdigest(),
        content_type="text/x-python",
        size=len(data),
        s3_key=f"k/{path}",
        kind=kind,
        path=path,
    )


def _skill(
    skill_id: str = "pdf",
    owner: str = OWNER.user_id,
    *,
    imported: bool = True,
    scripts: Optional[Dict[str, bytes]] = None,
) -> Any:
    files = {"fill.py": f"print('{skill_id}')".encode()} if scripts is None else scripts
    resources = [_ref("forms.md", b"# forms", kind="reference")]
    resources += [_ref(path, data) for path, data in files.items()]
    return SimpleNamespace(
        skill_id=skill_id,
        display_name=skill_id.upper(),
        owner_id=owner,
        resources=resources,
        import_source=SimpleNamespace(type="zip", provenance=lambda: {"type": "zip"}) if imported else None,
    )


def _binding(skill_id: str, version: Optional[int] = 2, **config: Any) -> AgentBinding:
    if version is not None:
        config["version"] = version
    return AgentBinding(kind="skill", ref=skill_id, config=config)


@pytest.fixture()
def table(roles_table, monkeypatch):
    monkeypatch.setattr(st, "_client", None)
    import boto3

    return boto3.resource("dynamodb", region_name="us-east-1").Table("test-app-roles")


@pytest.fixture()
def records(monkeypatch):
    """What ``load_skill_records`` returns: by ``id@n`` first, then by id."""
    by_ref: Dict[str, Any] = {}

    async def load(refs: List[str]) -> List[Any]:
        out = []
        for ref in refs:
            record = by_ref.get(ref) or by_ref.get(ref.split("@")[0])
            if record is not None:
                out.append(record)
        return out

    import apis.shared.skills.pinning as pinning

    monkeypatch.setattr(pinning, "load_skill_records", load)
    return by_ref


@pytest.fixture(autouse=True)
def names(monkeypatch):
    async def display(ids):
        return {i: f"Name of {i}" for i in ids if i}

    monkeypatch.setattr(rc, "_display_names", display)
    monkeypatch.setattr(sr, "_audit", lambda *a, **k: True)


def _unreviewed(before, after, **kw) -> List[Dict[str, Any]]:
    return asyncio.run(rc.unreviewed_script_skills(AGENT, before, after, OWNER.user_id, **kw))


def _review(skill: Any, version: int = 2) -> None:
    asyncio.run(
        sr.record_binding_reviews(
            AGENT, None, [_binding(skill.skill_id, version)], OWNER,
            scripts_reviewed={skill.skill_id: st.scripts_hash(skill)},
        )
    )


# ---------------------------------------------------------------------------
# When the sheet is needed
# ---------------------------------------------------------------------------


def test_a_newly_bound_imported_skill_with_scripts_needs_the_sheet(table, records):
    skill = _skill()
    records["pdf"] = skill
    [entry] = _unreviewed(None, [_binding("pdf")])
    assert entry["skillId"] == "pdf"
    assert entry["scriptsHash"] == st.scripts_hash(skill)
    assert (entry["version"], entry["fromVersion"], entry["reviewedAt"]) == (2, None, None)
    assert entry["scripts"] == [{"path": "scripts/fill.py", "size": len(b"print('pdf')"), "change": "new"}]
    assert entry["files"] == [{"path": "references/forms.md", "size": len(b"# forms")}]
    assert entry["provenance"] == {"type": "zip"}


def test_allowing_the_exact_hash_or_declining_answers_it(table, records):
    skill = _skill()
    records["pdf"] = skill
    after = [_binding("pdf")]
    assert _unreviewed(None, after, scripts_reviewed={"pdf": st.scripts_hash(skill)}) == []
    assert _unreviewed(None, after, scripts_declined=["pdf"]) == []
    # A stale hash (an edit raced the save) asks again, with the new set.
    [entry] = _unreviewed(None, after, scripts_reviewed={"pdf": "0" * 64})
    assert entry["scriptsHash"] == st.scripts_hash(skill)


def test_no_sheet_for_your_own_hand_written_scripts_a_catalog_skill_or_no_scripts(table, records):
    records["mine"] = _skill("mine", imported=False)
    records["catalog"] = _skill("catalog", owner="system")
    records["plain"] = _skill("plain", scripts={})
    assert _unreviewed(None, [_binding("mine"), _binding("catalog"), _binding("plain")]) == []
    # Someone else's hand-written skill does need it (direct sharing, §8.1).
    records["theirs"] = _skill("theirs", owner="someone-else", imported=False)
    assert [e["skillId"] for e in _unreviewed(None, [_binding("theirs")])] == ["theirs"]


def test_a_project_bindings_adder_is_the_binder(table, records):
    records["mine"] = _skill("mine", owner="adder-1", imported=False)
    assert _unreviewed(None, [_binding("mine", addedBy="adder-1")]) == []


def test_a_binding_the_save_leaves_alone_never_asks(table, records):
    records["pdf"] = _skill()
    same = [_binding("pdf", 2)]
    assert _unreviewed(same, same) == []
    # Nor a live, unpinned binding: there's no version to review.
    assert _unreviewed(None, [_binding("pdf", None)]) == []


def test_an_existing_review_at_the_new_hash_answers_it(table, records):
    skill = _skill()
    records["pdf"] = skill
    _review(skill, 2)
    # Moved to v3 with the same scripts (only instructions changed).
    assert _unreviewed([_binding("pdf", 2)], [_binding("pdf", 3)]) == []


def test_an_update_shows_what_changed_since_the_review(table, records):
    old = _skill(scripts={"fill.py": b"v1", "check.py": b"same", "gone.py": b"x"})
    new = _skill(scripts={"fill.py": b"v2", "check.py": b"same", "added.py": b"y"})
    records["pdf@2"], records["pdf@3"] = old, new
    _review(old, 2)
    [entry] = _unreviewed([_binding("pdf", 2)], [_binding("pdf", 3)])
    assert (entry["version"], entry["fromVersion"]) == (3, 2)
    assert entry["reviewedAt"]
    assert [(s["path"], s["change"]) for s in entry["scripts"]] == [
        ("scripts/added.py", "new"),
        ("scripts/check.py", "same"),
        ("scripts/fill.py", "changed"),
        ("scripts/gone.py", "removed"),
    ]


def test_an_update_from_a_version_nobody_reviewed_shows_every_script_as_new(table, records):
    records["pdf@2"] = _skill(scripts={"fill.py": b"v1"})
    records["pdf@3"] = _skill(scripts={"fill.py": b"v2"})
    [entry] = _unreviewed([_binding("pdf", 2)], [_binding("pdf", 3)])
    assert entry["fromVersion"] is None
    assert [s["change"] for s in entry["scripts"]] == ["new"]


def test_require_raises_with_names_never_ids(table, records):
    records["pdf"] = _skill(owner="author-7")
    with pytest.raises(rc.ScriptsUnreviewedError) as e:
        asyncio.run(rc.require_script_reviews(None, None, [_binding("pdf")], OWNER.user_id))
    detail = e.value.detail()
    assert detail["code"] == rc.SCRIPTS_UNREVIEWED_CODE
    [entry] = detail["skills"]
    assert entry["author"] == "Name of author-7"
    assert "authorId" not in entry
    assert "PDF" in detail["message"]


# ---------------------------------------------------------------------------
# Binding rows and badges
# ---------------------------------------------------------------------------


@pytest.fixture()
def live(monkeypatch):
    rows: Dict[str, Any] = {}

    class Repo:
        async def batch_get_skills(self, ids):
            return [rows[i] for i in ids if i in rows]

    import apis.shared.skills.repository as repository

    monkeypatch.setattr(repository, "get_skill_catalog_repository", lambda: Repo())
    return rows


def _status(bindings) -> Dict[str, rc.BindingScripts]:
    return {e.skill_id: e for e in asyncio.run(rc.binding_script_status(AGENT, bindings))}


def test_binding_status_states(table, records, live):
    allowed, mine, notyet, catalog, off = (
        _skill("allowed"),
        _skill("mine", imported=False),
        _skill("notyet"),
        _skill("catalog", owner="system"),
        _skill("off"),
    )
    for s in (allowed, mine, notyet, catalog, off):
        records[s.skill_id] = s
        live[s.skill_id] = s
    records["plain"] = live["plain"] = _skill("plain", scripts={})
    _review(allowed)
    _review(off)
    asyncio.run(sr.record_binding_reviews(AGENT, None, [_binding("mine")], OWNER))
    table.put_item(Item={"PK": st.skill_pk("off"), "SK": st.SWITCH_SK, "off": True, "by": "admin", "byUserId": "a"})

    status = _status([_binding(s) for s in ("allowed", "mine", "notyet", "catalog", "off", "plain")])
    assert {k: v.state for k, v in status.items()} == {
        "allowed": rc.STATE_ALLOWED,
        "mine": rc.STATE_AUTHORED,
        "notyet": rc.STATE_NOT_ALLOWED,
        "catalog": rc.STATE_ADMIN,
        "off": rc.STATE_OFF_BY_ADMIN,
    }
    assert status["allowed"].reviewed_by == OWNER.user_id
    assert not status["allowed"].scripts_changed


def test_binding_status_notices_the_live_scripts_moved_on(table, records, live):
    records["pdf@2"] = _skill(scripts={"fill.py": b"v1"})
    live["pdf"] = _skill(scripts={"fill.py": b"v2"})
    assert _status([_binding("pdf", 2)])["pdf"].scripts_changed


def test_a_member_sees_only_what_runs_for_them(table, records, live):
    records["allowed"] = live["allowed"] = _skill("allowed")
    records["notyet"] = live["notyet"] = _skill("notyet")
    _review(records["allowed"])
    entries = asyncio.run(rc.binding_script_status(AGENT, [_binding("allowed"), _binding("notyet")]))

    member = asyncio.run(rc.script_status_view(entries, full=False))
    assert [(r["skillId"], r["reviewer"]) for r in member] == [("allowed", f"Name of {OWNER.user_id}")]
    assert "scripts" not in member[0]

    editor = asyncio.run(rc.script_status_view(entries, full=True))
    assert [r["skillId"] for r in editor] == ["allowed", "notyet"]
    assert editor[1]["scripts"][0]["path"] == "scripts/fill.py"
