"""Whose skill scripts run (``apis.shared.skills.script_trust``; skill-code-sandbox §8)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from apis.shared.skills import script_trust as st
from apis.shared.skills.models import SkillResourceRef

INVOKER = "user-1"
AUTHOR = "author-1"
AGENT = "agent-1"


def _ref(path: str, data: bytes, kind: str = "script", content_hash: str | None = None) -> SkillResourceRef:
    return SkillResourceRef(
        filename=path.rsplit("/", 1)[-1],
        content_hash=hashlib.sha256(data).hexdigest() if content_hash is None else content_hash,
        content_type="text/x-python",
        size=len(data),
        s3_key=f"k/{path}",
        kind=kind,
        path=path,
    )


def _skill(owner: str = AUTHOR, *refs: SkillResourceRef, imported: bool = False) -> Any:
    resources = list(refs) or [_ref("fill.py", b"print(1)\n"), _ref("forms.md", b"# forms", kind="reference")]
    return SimpleNamespace(
        skill_id="pdf",
        owner_id=owner,
        resources=resources,
        import_source={"type": "github"} if imported else None,
    )


def _review(state: str = st.REVIEW_APPROVED) -> st.ScriptReview:
    return st.ScriptReview(reviewed_by=AUTHOR, state=state, version=3)


# ---------------------------------------------------------------------------
# The script set and its hash
# ---------------------------------------------------------------------------


def test_the_hash_covers_scripts_only_in_a_pinned_canonical_form():
    a = _ref("fill.py", b"print(1)\n")
    b = _ref("extract.py", b"print(2)\n")
    skill = _skill(AUTHOR, a, _ref("forms.md", b"# forms", kind="reference"), b)
    canonical = json.dumps(
        [["scripts/extract.py", b.content_hash], ["scripts/fill.py", a.content_hash]], separators=(",", ":")
    )
    # Pinned: a change here invalidates every review ever recorded.
    assert st.scripts_hash(skill) == hashlib.sha256(canonical.encode()).hexdigest()
    # References don't count; order in the manifest doesn't matter.
    assert st.scripts_hash(_skill(AUTHOR, b, a)) == st.scripts_hash(skill)


def test_a_known_script_set_has_a_known_hash():
    skill = _skill(AUTHOR, _ref("fill.py", b"", content_hash="0" * 64))
    assert st.scripts_hash(skill) == hashlib.sha256(
        b'[["scripts/fill.py","' + b"0" * 64 + b'"]]'
    ).hexdigest()
    assert st.short_hash(st.scripts_hash(skill)) == st.scripts_hash(skill)[:8]


def test_no_scripts_or_a_missing_hash_has_no_hash():
    assert st.scripts_hash(_skill(AUTHOR, _ref("forms.md", b"x", kind="reference"))) is None
    assert st.scripts_hash(_skill(AUTHOR, _ref("fill.py", b"x", content_hash=""))) is None


def test_changed_scripts_names_a_mismatch_and_a_missing_file():
    a, b = _ref("fill.py", b"one"), _ref("extract.py", b"two")
    skill = _skill(AUTHOR, a, b)
    staged = {"scripts/fill.py": a.content_hash, "scripts/extract.py": b.content_hash}
    assert st.changed_scripts(skill, staged) == []
    assert st.changed_scripts(skill, {**staged, "scripts/fill.py": "f" * 64}) == ["scripts/fill.py"]
    assert st.changed_scripts(skill, {"scripts/fill.py": a.content_hash}) == ["scripts/extract.py"]


# ---------------------------------------------------------------------------
# The verdict (§8.1, §8.7)
# ---------------------------------------------------------------------------


def test_own_imported_and_admin_skills_run_without_a_review():
    rows = st.TrustRows()
    assert st.script_verdict(_skill(INVOKER), INVOKER, None, rows).trusted_by == "own"
    assert st.script_verdict(_skill(INVOKER, imported=True), INVOKER, AGENT, rows).trusted_by == "imported"
    assert st.script_verdict(_skill("system"), INVOKER, None, rows).trusted_by == "admin"


def test_someone_elses_skill_needs_an_approved_review_for_this_scope():
    skill = _skill(AUTHOR)
    refused = st.script_verdict(skill, INVOKER, AGENT, st.TrustRows())
    assert (refused.allowed, refused.refused_reason) == (False, "not_reviewed")

    allowed = st.script_verdict(skill, INVOKER, AGENT, st.TrustRows(review=_review()))
    assert allowed.allowed and allowed.trusted_by == "reviewed"
    assert (allowed.reviewed_by, allowed.scope_id, allowed.version) == (AUTHOR, AGENT, 3)
    assert allowed.audit_fields() == {
        "scriptsHash": st.scripts_hash(skill),
        "trustedBy": "reviewed",
        "reviewedBy": AUTHOR,
        "scopeId": AGENT,
        "version": 3,
    }

    pending = st.script_verdict(skill, INVOKER, AGENT, st.TrustRows(review=_review(state="pending")))
    assert pending.refused_reason == "pending_admin_review"


def test_plain_chat_needs_an_acceptance():
    skill = _skill(AUTHOR)
    assert st.script_verdict(skill, INVOKER, None, st.TrustRows()).refused_reason == "not_trusted"
    accepted = st.script_verdict(skill, INVOKER, None, st.TrustRows(review=_review()))
    assert accepted.trusted_by == "accepted"


def test_a_switch_refuses_everyone_including_the_author():
    author_off = st.TrustRows(switch=st.ScriptSwitch(off=True, by="author"))
    admin_off = st.TrustRows(switch=st.ScriptSwitch(off=True, by="admin"), review=_review())
    assert st.script_verdict(_skill(INVOKER), INVOKER, None, author_off).refused_reason == "off_by_author"
    assert st.script_verdict(_skill("system"), INVOKER, None, admin_off).refused_reason == "off_by_admin"
    assert st.script_verdict(_skill(AUTHOR), INVOKER, AGENT, admin_off).refused_reason == "off_by_admin"


def test_a_switch_for_some_versions_leaves_the_others_alone():
    skill = _skill(AUTHOR)
    other = st.TrustRows(switch=st.ScriptSwitch(off=True, by="admin", hashes=("f" * 64,)), review=_review())
    this = st.TrustRows(
        switch=st.ScriptSwitch(off=True, by="admin", hashes=(st.scripts_hash(skill),)), review=_review()
    )
    assert st.script_verdict(skill, INVOKER, AGENT, other).allowed
    assert st.script_verdict(skill, INVOKER, AGENT, this).refused_reason == "off_by_admin"
    assert st.script_verdict(skill, INVOKER, AGENT, st.TrustRows(switch=st.ScriptSwitch(off=False))).refused_reason == "not_reviewed"


def test_someone_elses_unhashed_scripts_cannot_be_reviewed_but_own_ones_run():
    unhashed = (_ref("fill.py", b"x", content_hash=""),)
    assert st.script_verdict(_skill(AUTHOR, *unhashed), INVOKER, AGENT, st.TrustRows(review=_review())).refused_reason == "unreviewable"
    assert st.script_verdict(_skill(INVOKER, *unhashed), INVOKER, None, st.TrustRows()).allowed


def test_the_review_key_follows_the_scope_and_is_absent_when_no_path_needs_one():
    skill = _skill(AUTHOR)
    digest = st.scripts_hash(skill)
    assert st.review_key(skill, INVOKER, AGENT) == ("SKILL#pdf", f"SCRIPTS_REVIEW#AST#{AGENT}#{digest}")
    assert st.review_key(skill, INVOKER, None) == ("SKILL#pdf", f"SCRIPTS_REVIEW#USER#{INVOKER}#{digest}")
    assert st.review_key(_skill(INVOKER), INVOKER, AGENT) is None
    assert st.review_key(_skill("system"), INVOKER, AGENT) is None


# ---------------------------------------------------------------------------
# Reading the rows
# ---------------------------------------------------------------------------


class FakeDynamo:
    """A ``batch_get_item`` that answers from a dict and can hold keys back once."""

    def __init__(self, items: List[Dict[str, Any]], unprocessed_once: bool = False) -> None:
        self.items = items
        self.requests: List[Dict[str, Any]] = []
        self.unprocessed_once = unprocessed_once

    def batch_get_item(self, RequestItems: Dict[str, Any]) -> Dict[str, Any]:
        self.requests.append(RequestItems)
        (table, request), = RequestItems.items()
        wanted = [(k["PK"]["S"], k["SK"]["S"]) for k in request["Keys"]]
        if self.unprocessed_once:
            self.unprocessed_once = False
            return {"Responses": {table: []}, "UnprocessedKeys": RequestItems}
        found = [i for i in self.items if (i["PK"]["S"], i["SK"]["S"]) in wanted]
        return {"Responses": {table: found}, "UnprocessedKeys": {}}


def test_one_batch_get_reads_the_switch_and_this_scopes_review():
    skill = _skill(AUTHOR)
    digest = st.scripts_hash(skill)
    db = FakeDynamo(
        [
            {"PK": {"S": "SKILL#pdf"}, "SK": {"S": "SCRIPTS_SWITCH"}, "off": {"BOOL": False}},
            {
                "PK": {"S": "SKILL#pdf"},
                "SK": {"S": f"SCRIPTS_REVIEW#AST#{AGENT}#{digest}"},
                "reviewedBy": {"S": AUTHOR},
                "state": {"S": "approved"},
                "version": {"N": "4"},
            },
            # Another Agent's review of the same version never counts here.
            {"PK": {"S": "SKILL#pdf"}, "SK": {"S": f"SCRIPTS_REVIEW#AST#agent-2#{digest}"}, "reviewedBy": {"S": "x"}},
        ],
        unprocessed_once=True,
    )
    rows = st.read_trust_rows(skill, INVOKER, AGENT, client=db, table_name="roles")
    assert rows.switch == st.ScriptSwitch(off=False, by="author", by_user_id="", hashes=())
    assert rows.review == st.ScriptReview(reviewed_by=AUTHOR, state="approved", method="review", version=4)
    assert len(db.requests) == 2  # the held-back keys were asked for again
    assert len(db.requests[0]["roles"]["Keys"]) == 2


def test_an_own_skill_reads_only_the_switch():
    db = FakeDynamo([])
    st.read_trust_rows(_skill(INVOKER), INVOKER, AGENT, client=db, table_name="roles")
    assert db.requests[0]["roles"]["Keys"] == [{"PK": {"S": "SKILL#pdf"}, "SK": {"S": "SCRIPTS_SWITCH"}}]


def test_decide_refuses_when_the_rows_cannot_be_read(monkeypatch):
    def boom(*_: Any, **__: Any) -> st.TrustRows:
        raise RuntimeError("throttled")

    monkeypatch.setattr(st, "read_trust_rows", boom)
    verdict = asyncio.run(st.decide(_skill(INVOKER), INVOKER, None))
    assert (verdict.allowed, verdict.refused_reason) == (False, "check_failed")


def test_decide_reads_on_every_call(monkeypatch):
    calls: List[int] = []
    answers = [st.TrustRows(review=_review()), st.TrustRows()]

    def read(*_: Any, **__: Any) -> st.TrustRows:
        calls.append(1)
        return answers[len(calls) - 1]

    monkeypatch.setattr(st, "read_trust_rows", read)
    skill = _skill(AUTHOR)
    assert asyncio.run(st.decide(skill, INVOKER, AGENT)).allowed
    # The review was withdrawn between calls: the second call sees it.
    assert asyncio.run(st.decide(skill, INVOKER, AGENT)).refused_reason == "not_reviewed"
    assert len(calls) == 2


@pytest.mark.parametrize("reason", sorted({
    "not_reviewed", "pending_admin_review", "off_by_author", "off_by_admin",
    "not_trusted", "changed", "unreviewable", "check_failed",
}))
def test_every_refusal_says_nothing_ran_and_not_to_retype(reason):
    from agents.main_agent.skills.skill_scripts import trust_refusal

    text = trust_refusal(reason, "pdf", "scripts/fill.py", "project")
    assert text.startswith("'pdf/scripts/fill.py' didn't run")
    assert "Nothing ran. Don't copy the script" in text
    if reason == "not_reviewed":
        assert "this project" in text and "the project's owner" in text
