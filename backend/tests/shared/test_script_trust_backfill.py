"""The script-trust backfill (``backend/scripts/script_trust_backfill.py``; skill-code-sandbox PR-2d).

Against moto AppRoles and assistants tables and a moto skill-resources bucket, so
the pins it writes are the ones ``load_skill_records`` resolves, and the review
rows are the ones ``script_trust.read_trust_rows`` reads on a member's script call.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

import boto3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import script_trust_backfill as bf  # noqa: E402

from apis.shared.assistants.models import AgentBinding, AgentVersion  # noqa: E402
from apis.shared.skills import repository as repository_module  # noqa: E402
from apis.shared.skills import resource_store as resource_store_module  # noqa: E402
from apis.shared.skills import script_reviews as sr  # noqa: E402
from apis.shared.skills import script_trust as st  # noqa: E402
from apis.shared.skills.models import SkillDefinition, SkillResourceRef  # noqa: E402
from apis.shared.skills.pinning import load_skill_records  # noqa: E402
from apis.shared.skills.pins import pinned_ref  # noqa: E402
from apis.shared.skills.repository import SkillCatalogRepository  # noqa: E402
from apis.shared.skills.resource_store import SkillResourceStore, compute_content_hash  # noqa: E402

BUCKET = "test-skill-resources"
AUTHOR = "user-author-1"
EDITOR = "user-editor-1"
OTHER = "user-other-1"
MEMBER = "user-member-1"


@pytest.fixture()
def env(roles_table, assistants_table, monkeypatch):
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=BUCKET)
    store = SkillResourceStore(bucket_name=BUCKET, s3_client=s3)
    repo = SkillCatalogRepository(table_name="test-app-roles")
    monkeypatch.setattr(resource_store_module, "_store", store)
    monkeypatch.setattr(repository_module, "_repository_instance", repo)
    # The trust reader's client is per process; drop one built under another test's moto.
    monkeypatch.setattr(st, "_client", None)
    audits: List[Dict[str, Any]] = []

    def audit(action, actor, skill_id, after):
        audits.append({"action": action, "actor": actor.user_id, "skill": skill_id, "after": after})
        return True

    monkeypatch.setattr(sr, "_audit", audit)
    return {"store": store, "repo": repo, "roles": roles_table, "assistants": assistants_table, "audits": audits}


def _skill(env, skill_id: str, owner: str, *, script: bytes = b"print('fill')", hashed: bool = True) -> SkillDefinition:
    store = env["store"]
    resources = []
    for kind, path, body in (("reference", "forms.md", b"# forms"), ("script", "fill.py", script)):
        key = store.put(skill_id=skill_id, filename=path, content=body, content_type="text/plain", kind=kind)
        digest = compute_content_hash(body)
        resources.append(
            SkillResourceRef(
                filename=path,
                content_hash=digest if (hashed or kind != "script") else "",
                size=len(body),
                content_type="text/plain",
                s3_key=key,
                kind=kind,
            )
        )
    skill = SkillDefinition(
        skill_id=skill_id, display_name="Skill", description="d", instructions="Run it.", owner_id=owner,
        resources=resources,
    )
    return asyncio.run(env["repo"].create_skill(skill))


def _binding(skill_id: str, version: Optional[int] = None, **config: Any) -> Dict[str, Any]:
    if version is not None:
        config["version"] = version
    return {"kind": "skill", "ref": skill_id, "config": config}


def _assistant(env, assistant_id: str, owner: str, bindings: List[Dict[str, Any]], *, project: bool = False,
               published: Optional[int] = None) -> None:
    item: Dict[str, Any] = {
        "PK": f"AST#{assistant_id}", "SK": "METADATA", "assistantId": assistant_id, "ownerId": owner,
        "name": "A", "bindings": bindings,
    }
    if project:
        item["kind"] = "project"
    if published:
        item["listing"] = {"state": "published", "publishedVersion": published}
    env["assistants"].put_item(Item=item)


def _bindings(env, assistant_id: str) -> List[Dict[str, Any]]:
    item = env["assistants"].get_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})["Item"]
    return item["bindings"]


def _run(*, apply: bool = True) -> bf.Report:
    return asyncio.run(bf.run(apply_changes=apply))


def _by_skill(report: bf.Report, assistant_id: str) -> Dict[str, bf.Target]:
    return {t.skill_id: t for t in report.targets if t.assistant_id == assistant_id and t.source == "draft"}


def _member_verdict(skill_id: str, version: Optional[int], scope: str) -> st.ScriptVerdict:
    """What a member who isn't the author gets on a script call through ``scope``."""
    (record,) = asyncio.run(load_skill_records([pinned_ref(skill_id, version)]))
    rows = st.read_trust_rows(record, MEMBER, scope, table_name="test-app-roles")
    return st.script_verdict(record, MEMBER, scope, rows)


def _snapshot(env) -> Dict[str, Any]:
    s3 = boto3.client("s3")
    return {
        "roles": sorted((i["PK"], i["SK"], repr(sorted(i.items()))) for i in env["roles"].scan()["Items"]),
        "assistants": sorted(repr(sorted(i.items())) for i in env["assistants"].scan()["Items"]),
        "s3": sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])),
    }


# ---------------------------------------------------------------------------


def test_a_live_hand_written_agent_binding_is_pinned_and_reviewed(env):
    _skill(env, "redline", AUTHOR)
    _assistant(env, "agent-1", AUTHOR, [_binding("redline"), {"kind": "tool", "ref": "web_search", "config": {}}])
    assert _member_verdict("redline", None, "agent-1").refused_reason == "not_reviewed"

    report = _run()

    target = _by_skill(report, "agent-1")["redline"]
    assert (target.action, target.error, target.version) == ("pin+review", None, 1)
    bindings = _bindings(env, "agent-1")
    assert bindings[0]["config"] == {"version": 1}
    assert bindings[1] == {"kind": "tool", "ref": "web_search", "config": {}}
    verdict = _member_verdict("redline", 1, "agent-1")
    assert (verdict.allowed, verdict.trusted_by, verdict.reviewed_by, verdict.version) == (True, "reviewed", AUTHOR, 1)
    review = sr.read_review("redline", "agent-1", target.scripts_hash, table=env["roles"])
    assert review["method"] == "backfill"
    (audit,) = env["audits"]
    assert audit["action"] == "skill.scripts_reviewed" and audit["actor"] == "system"
    assert audit["after"]["method"] == "backfill" and audit["after"]["scopeType"] == "agent"
    assert audit["after"]["reviewedBy"] == AUTHOR and audit["after"]["version"] == 1
    assert not report.failed


def test_an_already_pinned_project_binding_is_reviewed_by_its_adder(env):
    _skill(env, "rubric", EDITOR)
    version = asyncio.run(bf_pin("rubric"))
    _assistant(env, "harness-1", AUTHOR, [_binding("rubric", version, addedBy=EDITOR)], project=True)

    report = _run()

    target = _by_skill(report, "harness-1")["rubric"]
    assert (target.action, target.reviewer, target.version) == ("review", EDITOR, version)
    assert _bindings(env, "harness-1")[0]["config"] == {"version": version, "addedBy": EDITOR}
    verdict = _member_verdict("rubric", version, "harness-1")
    assert (verdict.trusted_by, verdict.reviewed_by) == ("reviewed", EDITOR)
    assert env["audits"][0]["after"]["scopeType"] == "project"


def test_a_project_binding_with_no_adder_is_reviewed_by_the_harness_owner(env):
    _skill(env, "style", AUTHOR)
    _assistant(env, "harness-2", AUTHOR, [_binding("style")], project=True)

    report = _run()

    target = _by_skill(report, "harness-2")["style"]
    assert (target.action, target.reviewer) == ("pin+review", AUTHOR)
    assert _member_verdict("style", target.version, "harness-2").allowed


def test_a_binding_with_a_review_already_is_skipped(env):
    skill = _skill(env, "redline", AUTHOR)
    version = asyncio.run(bf_pin("redline"))
    _assistant(env, "harness-3", AUTHOR, [_binding("redline", version, addedBy=AUTHOR)], project=True)
    sr._put_review(
        "redline", "harness-3", st.scripts_hash(skill), reviewed_by=AUTHOR, method="authored", version=version,
        scripts=dict(st.script_entries(skill)), table=env["roles"],
    )

    report = _run()

    target = _by_skill(report, "harness-3")["redline"]
    assert (target.action, target.reason) == ("skip", "already_reviewed")
    assert sr.read_review("redline", "harness-3", st.scripts_hash(skill), table=env["roles"])["method"] == "authored"
    assert env["audits"] == []


def test_a_reviewer_who_did_not_write_the_skill_is_skipped(env):
    _skill(env, "redline", OTHER)
    _assistant(env, "agent-2", AUTHOR, [_binding("redline")])
    _assistant(env, "harness-4", AUTHOR, [_binding("redline", addedBy=EDITOR)], project=True)
    before = _snapshot(env)

    report = _run()

    for assistant_id in ("agent-2", "harness-4"):
        target = _by_skill(report, assistant_id)["redline"]
        assert (target.action, target.reason) == ("skip", "owner_mismatch")
    assert _snapshot(env) == before
    assert not report.failed


def test_a_catalog_skill_is_skipped(env):
    _skill(env, "pdf", "system")
    _assistant(env, "agent-3", AUTHOR, [_binding("pdf")])
    before = _snapshot(env)

    report = _run()

    target = _by_skill(report, "agent-3")["pdf"]
    assert (target.action, target.reason) == ("skip", "catalog")
    assert _snapshot(env) == before


def test_a_skill_without_scripts_is_not_listed(env):
    asyncio.run(env["repo"].create_skill(SkillDefinition(
        skill_id="plain", display_name="P", description="d", instructions="i", owner_id=AUTHOR,
    )))
    _assistant(env, "agent-4", AUTHOR, [_binding("plain")])

    assert _run().targets == []
    assert _bindings(env, "agent-4")[0]["config"] == {}


def test_a_missing_content_hash_is_filled_from_its_bytes(env):
    _skill(env, "legacy", AUTHOR, script=b"print('old')", hashed=False)
    assert st.scripts_hash(asyncio.run(env["repo"].get_skill("legacy"))) is None
    _assistant(env, "agent-5", AUTHOR, [_binding("legacy")])

    dry = _run(apply=False)
    assert _by_skill(dry, "agent-5")["legacy"].reason == "fills 1 hash(es)"

    report = _run()

    live = asyncio.run(env["repo"].get_skill("legacy"))
    script = next(r for r in live.resources if r.kind == "script")
    assert script.content_hash == compute_content_hash(b"print('old')")
    target = _by_skill(report, "agent-5")["legacy"]
    assert target.action == "pin+review" and target.error is None
    assert _member_verdict("legacy", target.version, "agent-5").allowed


def test_the_dry_run_writes_nothing(env):
    _skill(env, "redline", AUTHOR)
    _skill(env, "legacy", AUTHOR, hashed=False)
    _assistant(env, "agent-6", AUTHOR, [_binding("redline"), _binding("legacy")])
    before = _snapshot(env)

    report = _run(apply=False)

    assert {t.action for t in report.targets} == {"pin+review"}
    assert _snapshot(env) == before
    assert env["audits"] == []
    text = bf.render(report, applied=False)
    assert "Dry run: nothing was written" in text and "agent-6" in text and "Run it." not in text


def test_a_second_run_writes_nothing(env):
    _skill(env, "redline", AUTHOR)
    _skill(env, "legacy", AUTHOR, hashed=False)
    _assistant(env, "agent-7", AUTHOR, [_binding("redline")])
    _assistant(env, "harness-5", AUTHOR, [_binding("legacy", addedBy=AUTHOR)], project=True)
    _run()
    first_audits = len(env["audits"])
    after_first = _snapshot(env)

    again = _run()

    assert _snapshot(env) == after_first
    assert len(env["audits"]) == first_audits == 2
    assert {(t.action, t.reason) for t in again.targets} == {("skip", "already_reviewed")}


def test_a_published_agents_snapshot_is_covered(env):
    from apis.shared.assistants.version_repository import put_version

    _skill(env, "redline", AUTHOR)
    _skill(env, "rubric", AUTHOR)
    rubric_v = asyncio.run(bf_pin("rubric"))
    snapshot = AgentVersion(
        agent_id="agent-8", version=1, name="A", description="d", instructions="i",
        bindings=[AgentBinding(kind="skill", ref="redline"), AgentBinding(kind="skill", ref="rubric", config={"version": rubric_v})],
    )
    asyncio.run(put_version("agent-8", snapshot))
    # The draft has since dropped redline and moved on; members still run version 1.
    _assistant(env, "agent-8", AUTHOR, [_binding("rubric", rubric_v)], published=1)

    report = _run()

    published = {t.skill_id: t for t in report.targets if t.source == "published v1"}
    assert (published["redline"].action, published["redline"].reason) == ("review", "published_live")
    # The draft's rubric pin and the snapshot's are the same row, written once.
    assert published["rubric"].reason == "already_reviewed"
    assert _member_verdict("redline", None, "agent-8").allowed
    assert _member_verdict("rubric", rubric_v, "agent-8").allowed
    assert len(env["audits"]) == 2


def test_a_save_between_plan_and_apply_fails_the_pin_not_the_save(env):
    _skill(env, "redline", AUTHOR)
    _assistant(env, "agent-9", AUTHOR, [_binding("redline")])
    assistants = bf.scan_assistants(env["assistants"])
    report = asyncio.run(bf.plan(
        assistants, repository=env["repo"], store=env["store"], roles_table=env["roles"],
        load_version=lambda *_: None,
    ))
    # Someone saves the Agent before the apply runs.
    _assistant(env, "agent-9", AUTHOR, [_binding("redline"), {"kind": "tool", "ref": "web_search", "config": {}}])

    asyncio.run(bf.apply(
        report, assistants, repository=env["repo"], roles_table=env["roles"], assistants_table=env["assistants"]
    ))

    target = _by_skill(report, "agent-9")["redline"]
    assert target.error and target.error.startswith("binding write failed")
    assert report.failed
    assert len(_bindings(env, "agent-9")) == 2
    assert _member_verdict("redline", None, "agent-9").refused_reason == "not_reviewed"


def test_an_audit_write_failure_fails_the_run(env, monkeypatch):
    monkeypatch.setattr(sr, "_audit", lambda *args, **kwargs: False)
    _skill(env, "redline", AUTHOR)
    _assistant(env, "agent-10", AUTHOR, [_binding("redline")])

    report = _run()

    assert report.failed
    assert "review failed" in (_by_skill(report, "agent-10")["redline"].error or "")


def test_main_exits_non_zero_on_a_write_error(env, monkeypatch, capsys):
    monkeypatch.setattr(sr, "_audit", lambda *args, **kwargs: False)
    # ``main`` writes the names into the environment; let monkeypatch put them back.
    monkeypatch.setenv("DYNAMODB_AUDIT_LOG_TABLE_NAME", "unset")
    monkeypatch.setenv("S3_SKILL_RESOURCES_BUCKET_NAME", "unset")
    _skill(env, "redline", AUTHOR)
    _assistant(env, "agent-11", AUTHOR, [_binding("redline")])
    args = [
        "--assistants-table", "test-assistants", "--app-roles-table", "test-app-roles",
        "--audit-table", "test-audit", "--skill-resources-bucket", BUCKET,
    ]

    assert bf.main(args) == 0  # the dry run plans the write and writes nothing
    assert bf.main(args + ["--apply"]) == 1
    assert "ERROR" in capsys.readouterr().out


async def bf_pin(skill_id: str) -> int:
    """Pin the way a save does, before the backfill runs."""
    from apis.shared.skills.pinning import pin_current

    return (await pin_current(skill_id, pinned_by=AUTHOR)).version
