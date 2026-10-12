"""Who an edit to a user's skill reaches (docs/specs/skill-creator.md D9).

The lookup reads one partition: the skill owner's Agents, project harnesses
included. That is exact, because invoke-through only lets a private skill run for
someone else through an Agent its owner also owns, and a harness keeps the owner
it was created with. These pin the classification, the wording the approval card
shows, and that a failed lookup degrades to a warning rather than a claim that
the skill is bound nowhere.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.skills import reach
from apis.shared.skills.reach import (
    SkillBinding,
    SkillReach,
    bindings_from_items,
    lookup_skill_reach,
)


def _item(agent_id, name, *, bindings, visibility="PRIVATE", kind=None):
    item = {"assistantId": agent_id, "name": name, "visibility": visibility, "bindings": bindings}
    if kind:
        item["kind"] = kind
    return item


def _skill(ref, version=None):
    return {"kind": "skill", "ref": ref, "config": {"version": version} if version else {}}


class TestClassification:
    def test_finds_unpinned_and_pinned_bindings_across_agents_and_projects(self):
        items = [
            _item("a1", "Advisor", visibility="SHARED", bindings=[_skill("grant_review")]),
            _item("a2", "Scratch", bindings=[_skill("grant_review")]),
            _item("a3", "Grader", visibility="PUBLIC", bindings=[_skill("grant_review", 3)]),
            _item("h1", "BIO 101", kind="project", bindings=[_skill("grant_review")]),
            _item("a4", "Unrelated", visibility="SHARED", bindings=[_skill("other_skill")]),
            _item("a5", "Tools only", bindings=[{"kind": "tool", "ref": "grant_review"}]),
        ]
        found = bindings_from_items("grant_review", items)
        assert found == [
            SkillBinding("Advisor", is_project=False, shared=True, pinned_version=None),
            SkillBinding("Grader", is_project=False, shared=True, pinned_version=3),
            SkillBinding("Scratch", is_project=False, shared=False, pinned_version=None),
            SkillBinding("BIO 101", is_project=True, shared=True, pinned_version=None),
        ]

    def test_a_project_counts_as_shared_whatever_its_visibility(self):
        """A harness is PRIVATE on the record but runs for every member."""
        found = bindings_from_items(
            "s1", [_item("h1", "Team", kind="project", visibility="PRIVATE", bindings=[_skill("s1")])]
        )
        assert found[0].shared is True

    def test_a_pin_carried_on_the_ref_is_honored(self):
        found = bindings_from_items("s1", [_item("a1", "A", bindings=[_skill("s1@2")])])
        assert found[0].pinned_version == 2

    def test_rows_without_an_id_or_bindings_are_skipped(self):
        assert bindings_from_items("s1", [{"name": "x"}, _item("a1", "A", bindings=None)]) == []


class TestSummary:
    def test_unbound(self):
        assert "not used by any of your agents or projects" in SkillReach("s1").summary()

    def test_names_who_gets_the_new_version_and_who_is_pinned(self):
        text = SkillReach(
            "s1",
            [
                SkillBinding("Advisor", False, True, None),
                SkillBinding("Helper", False, True, None),
                SkillBinding("BIO 101", True, True, None),
                SkillBinding("Scratch", False, False, None),
                SkillBinding("Grader", False, True, 3),
                SkillBinding("Team", True, True, 1),
            ],
        ).summary()
        assert 'runs unpinned in 2 shared agents ("Advisor", "Helper") and 1 project ("BIO 101")' in text
        assert "use the new version on their next turn" in text
        assert '1 of your private agent ("Scratch")' in text
        assert 'Pinned, so unaffected: "Grader" (v3), project "Team" (v1).' in text

    def test_long_lists_are_cut(self):
        many = [SkillBinding(f"Agent {i}", False, True, None) for i in range(8)]
        assert "and 3 more" in SkillReach("s1", many).summary()

    def test_a_failed_lookup_warns_instead_of_claiming_nothing(self):
        text = SkillReach("s1", complete=False, error="ClientError").summary()
        assert "Could not check" in text
        assert "not used by any" not in text

    def test_an_incomplete_lookup_says_so(self):
        text = SkillReach("s1", complete=False).summary()
        assert f"first {reach.MAX_PAGES} pages" in text


class TestLookup:
    @pytest.fixture()
    def table(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "test-assistants")
        with mock_aws():
            boto3.client("dynamodb", region_name="us-east-1").create_table(
                TableName="test-assistants",
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": a, "AttributeType": "S"}
                    for a in ("PK", "SK", "GSI_PK", "GSI_SK")
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "OwnerStatusIndex",
                        "KeySchema": [
                            {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                            {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
            )
            yield boto3.resource("dynamodb", region_name="us-east-1").Table("test-assistants")

    def _put(self, table, owner, agent_id, name, **kw):
        from decimal import Decimal

        item = _item(agent_id, name, **kw)
        for b in item["bindings"]:
            if b.get("config", {}).get("version"):
                b["config"]["version"] = Decimal(b["config"]["version"])
        table.put_item(
            Item={
                "PK": f"AST#{agent_id}",
                "SK": "METADATA",
                "GSI_PK": f"OWNER#{owner}",
                "GSI_SK": f"STATUS#COMPLETE#CREATED#{agent_id}",
                "instructions": "long " * 100,
                **item,
            }
        )

    def test_reads_only_the_owners_partition_including_harnesses_and_drafts(self, table):
        self._put(table, "u1", "a1", "Advisor", visibility="SHARED", bindings=[_skill("s1")])
        self._put(table, "u1", "h1", "Team", kind="project", bindings=[_skill("s1", 2)])
        self._put(table, "u2", "x1", "Not mine", visibility="SHARED", bindings=[_skill("s1")])

        result = lookup_skill_reach("s1", "u1")

        assert result.complete and result.error is None
        assert [(b.name, b.pinned_version) for b in result.bindings] == [
            ("Advisor", None),
            ("Team", 2),
        ]

    def test_without_the_table_it_reports_rather_than_guesses(self, monkeypatch):
        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME", raising=False)
        result = lookup_skill_reach("s1", "u1")
        assert result.error and "Could not check" in result.summary()

    def test_a_query_failure_never_raises(self, monkeypatch):
        monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "t")

        def boom(*a, **k):
            raise RuntimeError("throttled")

        monkeypatch.setattr(reach, "_query_owned_assistant_bindings", boom)
        result = lookup_skill_reach("s1", "u1")
        assert result.error == "RuntimeError"
        assert not result.complete
