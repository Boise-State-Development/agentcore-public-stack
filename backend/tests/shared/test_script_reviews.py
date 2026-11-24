"""Recording who allowed a skill's scripts (``apis.shared.skills.script_reviews``; skill-code-sandbox §8.4–§8.5).

Against a moto AppRoles table, so the rows these writers put are the rows
``script_trust.read_trust_rows`` reads on a script call.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from apis.shared.assistants.models import AgentBinding
from apis.shared.auth.models import User
from apis.shared.skills import script_reviews as sr
from apis.shared.skills import script_trust as st
from apis.shared.skills.models import SkillResourceRef

AGENT = "agent-1"
OWNER = User(email="owner@example.com", user_id="owner-1", name="Owner", roles=[])
EDITOR = User(email="editor@example.com", user_id="editor-1", name="Editor", roles=[])


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


def _skill(skill_id: str = "pdf", owner: str = OWNER.user_id, *, imported: bool = False, scripts: bool = True) -> Any:
    resources = [_ref("forms.md", b"# forms", kind="reference")]
    if scripts:
        resources.append(_ref("fill.py", f"print('{skill_id}')".encode()))
    return SimpleNamespace(
        skill_id=skill_id,
        owner_id=owner,
        resources=resources,
        import_source=SimpleNamespace(type="github") if imported else None,
    )


def _binding(skill_id: str, version: int | None = 2, **config: Any) -> AgentBinding:
    if version is not None:
        config["version"] = version
    return AgentBinding(kind="skill", ref=skill_id, config=config)


@pytest.fixture()
def table(roles_table, monkeypatch):
    # The reader's client is per process; drop one built under another test's moto.
    monkeypatch.setattr(st, "_client", None)
    import boto3

    return boto3.resource("dynamodb", region_name="us-east-1").Table("test-app-roles")


@pytest.fixture()
def records(monkeypatch):
    """What ``load_skill_records`` returns, by skill id."""
    by_id: Dict[str, Any] = {}

    async def load(refs: List[str]) -> List[Any]:
        return [by_id[ref.split("@")[0]] for ref in refs if ref.split("@")[0] in by_id]

    import apis.shared.skills.pinning as pinning

    monkeypatch.setattr(pinning, "load_skill_records", load)
    return by_id


@pytest.fixture()
def audits(monkeypatch):
    seen: List[Dict[str, Any]] = []

    def record(action, actor, skill_id, after):
        seen.append({"action": action, "actor": actor.user_id, "skill": skill_id, "after": after})

    monkeypatch.setattr(sr, "_audit", record)
    return seen


def _record(*args: Any, **kwargs: Any) -> List[sr.RecordedReview]:
    return asyncio.run(sr.record_binding_reviews(*args, **kwargs))


def _member_verdict(skill: Any, scope: str = AGENT) -> st.ScriptVerdict:
    rows = st.read_trust_rows(skill, "member-1", scope, table_name="test-app-roles")
    return st.script_verdict(skill, "member-1", scope, rows)


# ---------------------------------------------------------------------------
# Reviews on save
# ---------------------------------------------------------------------------


def test_binding_your_own_hand_written_skill_is_an_authored_review(table, records, audits):
    skill = _skill()
    records["pdf"] = skill
    assert _member_verdict(skill).refused_reason == "not_reviewed"

    written = _record(AGENT, None, [_binding("pdf")], OWNER)

    digest = st.scripts_hash(skill)
    assert written == [sr.RecordedReview("pdf", digest, "authored", 2)]
    verdict = _member_verdict(skill)
    assert (verdict.trusted_by, verdict.reviewed_by, verdict.version) == ("reviewed", OWNER.user_id, 2)
    (audit,) = audits
    assert audit["action"] == "skill.scripts_reviewed"
    assert audit["after"]["method"] == "authored" and audit["after"]["scriptsHash"] == digest
    assert audit["after"]["scripts"] == {"scripts/fill.py": skill.resources[1].content_hash}


def test_an_imported_skill_needs_the_hash_the_saver_was_shown(table, records, audits):
    skill = _skill(imported=True)
    records["pdf"] = skill
    assert _record(AGENT, None, [_binding("pdf")], OWNER) == []
    assert _record(AGENT, None, [_binding("pdf")], OWNER, scripts_reviewed={"pdf": "f" * 64}) == []
    assert _member_verdict(skill).refused_reason == "not_reviewed"

    written = _record(AGENT, None, [_binding("pdf")], OWNER, scripts_reviewed={"pdf": st.scripts_hash(skill)})
    assert [w.method for w in written] == ["review"]
    assert _member_verdict(skill).allowed


def test_someone_elses_hand_written_skill_is_not_authored_by_the_saver(table, records, audits):
    records["pdf"] = _skill(owner=OWNER.user_id)
    assert _record(AGENT, None, [_binding("pdf")], EDITOR) == []


def test_a_projects_adder_is_the_binder(table, records, audits):
    records["pdf"] = _skill(owner=EDITOR.user_id)
    written = _record(AGENT, None, [_binding("pdf", addedBy=EDITOR.user_id)], OWNER, scope_kind="project")
    assert [(w.method, w.skill_id) for w in written] == [("authored", "pdf")]
    assert _member_verdict(records["pdf"]).reviewed_by == EDITOR.user_id
    assert audits[0]["after"]["scopeType"] == "project"


def test_catalog_skills_unpinned_bindings_and_skills_without_scripts_get_no_review(table, records, audits):
    records["catalog"] = _skill("catalog", owner="system")
    records["plain"] = _skill("plain", scripts=False)
    records["live"] = _skill("live")
    bindings = [_binding("catalog"), _binding("plain"), _binding("live", version=None)]
    assert _record(AGENT, None, bindings, OWNER) == []
    assert audits == []


def test_a_review_already_recorded_is_not_written_again(table, records, audits):
    records["pdf"] = _skill()
    _record(AGENT, None, [_binding("pdf")], OWNER)
    assert _record(AGENT, [_binding("pdf")], [_binding("pdf")], OWNER) == []
    assert len(audits) == 1


def test_unbinding_withdraws_only_that_assistants_reviews(table, records, audits):
    skill = _skill()
    records["pdf"] = skill
    _record(AGENT, None, [_binding("pdf")], OWNER)
    _record("agent-2", None, [_binding("pdf")], OWNER)

    assert _record(AGENT, [_binding("pdf")], [], OWNER) == []

    assert _member_verdict(skill, AGENT).refused_reason == "not_reviewed"
    assert _member_verdict(skill, "agent-2").allowed
    withdrawn = [a for a in audits if a["action"] == "skill.scripts_review_withdrawn"]
    assert withdrawn == [{
        "action": "skill.scripts_review_withdrawn",
        "actor": OWNER.user_id,
        "skill": "pdf",
        "after": {"scopeType": "agent", "scopeId": AGENT, "scriptsHashes": [st.scripts_hash(skill)], "reason": "unbound"},
    }]


def test_a_failed_write_never_fails_the_save(monkeypatch, records):
    records["pdf"] = _skill()

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("throttled")

    monkeypatch.setattr(sr, "_existing_reviews", boom)
    assert _record(AGENT, None, [_binding("pdf")], OWNER) == []


# ---------------------------------------------------------------------------
# The author's switch
# ---------------------------------------------------------------------------


def test_the_authors_switch_stops_members_and_comes_back_on(table, records, audits):
    skill = _skill()
    records["pdf"] = skill
    _record(AGENT, None, [_binding("pdf")], OWNER)

    asyncio.run(sr.set_author_switch("pdf", OWNER, off=True, reason="fixing a bug"))
    assert _member_verdict(skill).refused_reason == "off_by_author"
    asyncio.run(sr.set_author_switch("pdf", OWNER, off=True))  # no-op, no second record

    asyncio.run(sr.set_author_switch("pdf", OWNER, off=False))
    assert _member_verdict(skill).allowed
    switched = [a["after"] for a in audits if a["action"] == "skill.scripts_switched"]
    assert switched == [{"off": True, "by": "author"}, {"off": False, "by": "author"}]


def test_turning_on_scripts_that_were_never_off_writes_nothing(table, audits):
    asyncio.run(sr.set_author_switch("pdf", OWNER, off=False))
    assert sr.read_switch("pdf") is None and audits == []


def test_an_admins_switch_cannot_be_undone_by_the_author(table, audits):
    table.put_item(Item={"PK": "SKILL#pdf", "SK": "SCRIPTS_SWITCH", "off": True, "by": "admin", "byUserId": "admin-1"})
    with pytest.raises(sr.ScriptSwitchLockedError):
        asyncio.run(sr.set_author_switch("pdf", OWNER, off=False))
    assert sr.read_switch("pdf").by == "admin"


# ---------------------------------------------------------------------------
# Import provenance and pinning
# ---------------------------------------------------------------------------


def test_an_import_records_its_scripts_on_the_row_but_not_in_responses():
    from apis.shared.skills.models import SkillImportSource

    resources = [_ref("fill.py", b"x"), _ref("forms.md", b"y", kind="reference")]
    extra = st.scripts_provenance(resources)
    assert extra == {
        "scripts_hash": st.scripts_hash(SimpleNamespace(resources=resources)),
        "scripts": {"scripts/fill.py": resources[0].content_hash},
    }
    assert st.scripts_provenance([_ref("forms.md", b"y", kind="reference")]) == {}

    source = SkillImportSource(type="zip", bundle_hash="b", imported_at="t").model_copy(update=extra)
    item = source.to_item()
    assert item["scriptsHash"] == extra["scripts_hash"] and item["scripts"] == extra["scripts"]
    assert "scriptsHash" not in source.model_dump(by_alias=True)
    assert SkillImportSource.model_validate(item).scripts_hash == extra["scripts_hash"]


def test_an_agent_pins_a_hand_written_skill_once_it_has_scripts(monkeypatch):
    import apis.shared.skills.pinning as pinning

    rows = {"scripted": _skill("scripted"), "plain": _skill("plain", scripts=False)}
    for row in rows.values():
        row.import_source = None

    class Repo:
        async def batch_get_skills(self, ids):
            return [rows[i] for i in ids]

    async def pin(skill_id, *, pinned_by):
        return SimpleNamespace(version=7)

    monkeypatch.setattr(pinning, "get_skill_catalog_repository", lambda: Repo())
    monkeypatch.setattr(pinning, "pin_current", pin)
    out = asyncio.run(
        pinning.pin_imported_skill_bindings(
            [_binding("scripted", version=None), _binding("plain", version=None)], pinned_by=OWNER.user_id
        )
    )
    assert {b.ref: b.config.get("version") for b in out} == {"scripted": 7, "plain": None}
