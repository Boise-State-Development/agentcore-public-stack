"""Tests for scripts/audit_user_duplicates.py (legacy duplicate PROFILE rows)."""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import audit_user_duplicates as audit  # noqa: E402
from apis.shared.users.repository import UserRepository  # noqa: E402

REGION = "us-east-1"
PREFIX = "test"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

LIVE = "a1b2c3d4-0000-4000-8000-000000000001"
LEGACY = "100000001"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _profile_item(user_id, email, last_login, **extra):
    item = {
        "PK": f"USER#{user_id}", "SK": "PROFILE", "userId": user_id, "email": email,
        "name": "Prof", "roles": [], "emailDomain": email.split("@")[1],
        "createdAt": "2026-03-01T00:00:00Z", "status": "active",
        "GSI3PK": "STATUS#active", "GSI3SK": last_login,
    }
    if last_login is not None:
        item["lastLoginAt"] = last_login
    item.update(extra)
    return item


def _create_tables(ddb):
    """Every TABLE_SOURCES table, PK/SK plus the GSIs QUERY_CHECKS reads."""
    names = {}
    for src in audit.TABLE_SOURCES:
        name = f"{PREFIX}-{src.suffix or src.logical}"
        gsis = {c.index: c.key for c in audit.QUERY_CHECKS if c.table == src.logical and c.index}
        if src.logical == "users":
            gsis["EmailIndex"] = "email"
        attrs = {"PK", "SK", *gsis.values()}
        kwargs = {
            "TableName": name,
            "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"},
                          {"AttributeName": "SK", "KeyType": "RANGE"}],
            "AttributeDefinitions": [{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            "BillingMode": "PAY_PER_REQUEST",
        }
        if gsis:
            kwargs["GlobalSecondaryIndexes"] = [
                {"IndexName": idx, "KeySchema": [{"AttributeName": key, "KeyType": "HASH"}],
                 "Projection": {"ProjectionType": "ALL"}}
                for idx, key in gsis.items()
            ]
        ddb.create_table(**kwargs)
        names[src.logical] = name
    return names


@pytest.fixture
def stack():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ssm = boto3.client("ssm", region_name=REGION)
        s3 = boto3.client("s3", region_name=REGION)
        names = _create_tables(ddb)
        for src in audit.TABLE_SOURCES:
            if src.ssm:
                ssm.put_parameter(Name=f"/{PREFIX}{src.ssm}", Value=names[src.logical], Type="String")
        for bucket in audit.BUCKET_SOURCES:
            s3.create_bucket(Bucket=f"{PREFIX}-{bucket.logical}")
            ssm.put_parameter(Name=f"/{PREFIX}{bucket.ssm}", Value=f"{PREFIX}-{bucket.logical}", Type="String")
        ssm.put_parameter(Name=f"/{PREFIX}{audit.SSM_USER_POOL_ID}", Value="pool-1", Type="String")
        ssm.put_parameter(Name=f"/{PREFIX}{audit.SSM_MEMORY_ID}", Value="mem-1", Type="String")

        cognito = MagicMock()
        cognito.list_users.return_value = {"Users": [{"Username": "x"}]}
        agentcore = MagicMock()
        agentcore.list_sessions.return_value = {"sessionSummaries": []}
        clients = audit.Clients(
            dynamodb=boto3.resource("dynamodb", region_name=REGION), s3=s3, ssm=ssm,
            cognito=cognito, agentcore=agentcore,
        )
        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {"clients": clients, "tables": {k: resource.Table(v) for k, v in names.items()}, "s3": s3, "ssm": ssm}


def _args(tmp_path, *extra):
    return audit.parse_args(["--project-prefix", PREFIX, "--region", REGION,
                             "--out-dir", str(tmp_path), "--sleep", "0", *extra])


def _report(tmp_path):
    [path] = tmp_path.glob("user-duplicates-*.json")
    return json.loads(path.read_text())


def _only_group(report):
    [group] = report["groups"]
    return group


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


class TestBuildGroups:
    def test_numeric_plus_uuid_is_a_legacy_pair_live_first(self):
        groups, orphans, counters = audit.build_groups([
            _profile_item(LEGACY, "a@x.edu", "2026-04-15T00:00:00Z"),
            _profile_item(LIVE, "a@x.edu", "2026-09-20T00:00:00Z"),
        ])
        [g] = groups
        assert g.kind == "legacy_pair"
        assert [r.user_id for r in g.rows] == [LIVE, LEGACY]
        assert [r.role for r in g.rows] == ["live", "stale"]
        assert counters == {"profileRows": 2, "numericProfileRows": 1, "unparseableRows": 0}
        assert orphans == []

    def test_email_case_is_folded(self):
        groups, _, _ = audit.build_groups([
            _profile_item(LEGACY, "A@X.edu", "2026-04-15T00:00:00Z"),
            _profile_item(LIVE, "a@x.edu", "2026-09-20T00:00:00Z"),
        ])
        assert len(groups) == 1

    @pytest.mark.parametrize("items", [
        # two uuids (Cognito re-creation)
        [("aaaa-1", "2026-06-01T00:00:00Z"), ("bbbb-2", "2026-09-01T00:00:00Z")],
        # three rows
        [(LEGACY, "2026-04-01T00:00:00Z"), ("aaaa-1", "2026-06-01T00:00:00Z"), ("bbbb-2", "2026-09-01T00:00:00Z")],
        # the numeric row signed in more recently than its uuid twin
        [(LEGACY, "2026-09-21T00:00:00Z"), (LIVE, "2026-09-20T00:00:00Z")],
    ])
    def test_anything_else_needs_review(self, items):
        groups, _, _ = audit.build_groups([_profile_item(uid, "a@x.edu", ts) for uid, ts in items])
        [g] = groups
        assert g.kind == "needs_review"

    def test_lone_numeric_row_is_an_orphan_and_lone_uuid_is_ignored(self):
        groups, orphans, _ = audit.build_groups([
            _profile_item(LEGACY, "gone@x.edu", "2026-04-01T00:00:00Z"),
            _profile_item(LIVE, "fine@x.edu", "2026-09-01T00:00:00Z"),
        ])
        assert groups == []
        assert [r.user_id for r in orphans] == [LEGACY]

    def test_email_filter_and_unparseable_rows(self):
        groups, _, counters = audit.build_groups([
            _profile_item(LEGACY, "a@x.edu", "2026-04-15T00:00:00Z"),
            _profile_item(LIVE, "a@x.edu", "2026-09-20T00:00:00Z"),
            _profile_item("222", "b@x.edu", "2026-04-15T00:00:00Z"),
            _profile_item("bbbb", "b@x.edu", "2026-09-20T00:00:00Z"),
            {"PK": "USER#broken", "SK": "PROFILE"},
        ], only_emails={"b@x.edu"})
        assert [g.email for g in groups] == ["b@x.edu"]
        assert counters["unparseableRows"] == 1

    @pytest.mark.asyncio
    async def test_live_row_matches_what_the_api_returns(self):
        items = [
            _profile_item("bbbb-2", "a@x.edu", "2026-09-20T12:00:00Z"),
            _profile_item(LEGACY, "a@x.edu", "2026-09-20T12:00:00Z"),
            _profile_item("aaaa-1", "a@x.edu", "2026-09-20T12:00:00.500000Z"),
        ]
        [g], _, _ = audit.build_groups(items)

        repo = UserRepository(table_name="")
        repo._enabled = True
        repo.table = MagicMock()
        repo.table.query.return_value = {"Items": items}
        api_pick = await repo.get_user_by_email("a@x.edu")

        assert g.live_user_id == api_pick.user_id == "aaaa-1"


# ---------------------------------------------------------------------------
# Token matching for the deep scan
# ---------------------------------------------------------------------------


class TestReferences:
    @pytest.mark.parametrize("value", [
        LEGACY, f"USER#{LEGACY}", f"COST#2026-04#USER#{LEGACY}", f"ELIG#pub-1#{LEGACY}",
        f"user-files/{LEGACY}/doc.pdf", [f"OWNER#{LEGACY}"], {"members": {LEGACY: "editor"}},
    ])
    def test_matches(self, value):
        assert audit._references(value, {LEGACY}) == {LEGACY}

    @pytest.mark.parametrize("value", [
        f"0.{LEGACY}1", f"{LEGACY}9", f"2026-04-15T{LEGACY}", "USER#other", 100000001, None,
    ])
    def test_does_not_match_digits_inside_other_values(self, value):
        assert audit._references(value, {LEGACY}) == set()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_everything_found(self, stack):
        env = audit.discover(stack["clients"], PREFIX)
        assert all(v == "ok" for v in env.statuses.values()), env.statuses
        assert env.user_pool_id == "pool-1" and env.memory_id == "mem-1"

    def test_missing_required_is_an_error_optional_is_not_deployed(self, stack):
        stack["ssm"].delete_parameter(Name=f"/{PREFIX}/auth/api-keys-table-name")
        stack["ssm"].delete_parameter(Name=f"/{PREFIX}/fine-tuning/jobs-table-name")
        stack["tables"]["projects"].delete()

        env = audit.discover(stack["clients"], PREFIX)

        assert env.statuses["api-keys"].startswith("error")
        assert env.statuses["fine-tuning-jobs"] == "not_deployed"
        assert env.statuses["projects"] == "not_deployed"


# ---------------------------------------------------------------------------
# End to end: audit, mark, delete
# ---------------------------------------------------------------------------


def _seed_pair(stack, email="prof@x.edu", legacy=LEGACY, live=LIVE):
    users = stack["tables"]["users"]
    users.put_item(Item=_profile_item(legacy, email, "2026-04-15T00:00:00Z"))
    # The live row is created when the person first signs in through Cognito:
    # that is their cutover, and anything newer under the old id means it's live.
    users.put_item(Item=_profile_item(live, email, "2026-09-20T00:00:00Z", createdAt="2026-04-16T09:00:00Z"))


class TestAudit:
    def test_unreferenced_stale_row_would_be_marked_and_nothing_is_written(self, stack, tmp_path):
        _seed_pair(stack)

        code = audit.run(_args(tmp_path), stack["clients"], "123", now=NOW)

        assert code == 0
        report = _report(tmp_path)
        live, stale = _only_group(report)["rows"]
        assert (stale["verdict"], stale["action"]) == ("unreferenced", "would mark")
        assert report["summary"]["eligible"] == 1 and report["summary"]["written"] == 0
        assert "mergedInto" not in stack["tables"]["users"].get_item(
            Key={"PK": f"USER#{LEGACY}", "SK": "PROFILE"})["Item"]
        assert (tmp_path / Path(next(tmp_path.glob("*.json")).stem + ".md")).exists()

    def test_references_on_indexed_paths_block_the_row(self, stack, tmp_path):
        _seed_pair(stack)
        t = stack["tables"]
        t["api-keys"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "KEY#k1", "userId": LEGACY,
                                     "expiresAt": "2026-05-01T00:00:00+00:00", "lastUsedAt": "2026-04-10T00:00:00Z"})
        t["sessions-metadata"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "S#s1"})
        t["sessions-metadata"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "C#2026-04-01#x"})
        t["sessions-metadata"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "C#2026-04-02#y"})
        t["user-quotas"].put_item(Item={"PK": "OVERRIDE#o1", "SK": "META", "GSI4PK": f"USER#{LEGACY}"})
        t["rag-assistants"].put_item(Item={"PK": "AST#a1", "SK": "META", "GSI_PK": f"OWNER#{LEGACY}"})
        stack["s3"].put_object(Bucket=f"{PREFIX}-user-file-uploads", Key=f"user-files/{LEGACY}/a.pdf", Body=b"x")

        audit.run(_args(tmp_path), stack["clients"], "123", now=NOW)

        _, stale = _only_group(_report(tmp_path))["rows"]
        assert stale["verdict"] == "referenced"
        assert stale["action"] == "skip: verdict is referenced"
        assert stale["in_use"] == []
        assert stale["last_activity_at"] == "2026-04-10T00:00:00Z"
        refs = stale["references"]
        assert refs["api-keys"] == 1
        assert refs["sessions-metadata"] == 3
        assert stale["breakdown"]["sessions-metadata"] == {"S": 1, "C": 2}
        assert refs["quota-overrides"] == 1
        assert refs["agents-owned"] == 1
        assert refs["s3:user-file-uploads/user-files/"] == 1

    def test_memory_sessions_block_the_row(self, stack, tmp_path):
        _seed_pair(stack)
        stack["clients"].agentcore.list_sessions.return_value = {"sessionSummaries": [{"sessionId": "s"}]}

        audit.run(_args(tmp_path), stack["clients"], "123", now=NOW)

        _, stale = _only_group(_report(tmp_path))["rows"]
        assert stale["references"]["agentcore-memory-sessions"] == 1
        stack["clients"].agentcore.list_sessions.assert_called_with(memoryId="mem-1", actorId=LEGACY, maxResults=1)

    def test_deep_scan_finds_unindexed_references(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["projects"].put_item(Item={
            "PK": "PROJECT#p1", "SK": f"COST#2026-04#USER#{LEGACY}", "cost": "0.100000001",
        })
        stack["tables"]["system-cost-rollup"].put_item(Item={"PK": "ACTIVE#DAILY#2026-04-01", "SK": LEGACY})
        # One row naming the id twice (PK and userId) is one reference, not two.
        stack["tables"]["app-roles"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "TOOL_PREFERENCES", "userId": LEGACY})

        audit.run(_args(tmp_path, "--deep"), stack["clients"], "123", now=NOW)

        _, stale = _only_group(_report(tmp_path))["rows"]
        assert stale["verdict"] == "referenced"
        assert stale["references"]["deep:projects"] == 1
        assert stale["references"]["deep:system-cost-rollup"] == 1
        assert stale["references"]["deep:app-roles"] == 1
        assert f"USER#{LEGACY} / TOOL_PREFERENCES (PK, userId)" in " ".join(stale["deep_samples"])
        assert any("COST#2026-04#USER#" in s for s in stale["deep_samples"])

    def test_a_failed_check_makes_the_row_incomplete_not_unreferenced(self, stack, tmp_path):
        _seed_pair(stack)
        stack["ssm"].delete_parameter(Name=f"/{PREFIX}/auth/api-keys-table-name")

        code = audit.run(_args(tmp_path), stack["clients"], "123", now=NOW)

        assert code == 1
        _, stale = _only_group(_report(tmp_path))["rows"]
        assert stale["verdict"] == "incomplete"
        assert stale["action"] == "skip: verdict is incomplete"

    def test_a_waived_check_is_recorded_and_does_not_block(self, stack, tmp_path):
        _seed_pair(stack)
        stack["ssm"].delete_parameter(Name=f"/{PREFIX}/auth/api-keys-table-name")

        code = audit.run(_args(tmp_path, "--waive-check", "api-keys"), stack["clients"], "123", now=NOW)

        assert code == 0
        report = _report(tmp_path)
        assert {c["check_id"]: c["status"] for c in report["checks"]}["api-keys"] == "waived"
        assert _only_group(report)["rows"][1]["verdict"] == "unreferenced"

    def test_needs_review_group_is_reported_with_cognito_and_never_acted_on(self, stack, tmp_path):
        users = stack["tables"]["users"]
        users.put_item(Item=_profile_item("aaaa-old", "re@x.edu", "2026-06-01T00:00:00Z"))
        users.put_item(Item=_profile_item("bbbb-new", "re@x.edu", "2026-09-01T00:00:00Z"))
        stack["clients"].cognito.list_users.side_effect = lambda **kw: (
            {"Users": [{}]} if "bbbb-new" in kw["Filter"] else {"Users": []})

        audit.run(_args(tmp_path, "--apply", "mark", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW)

        group = _only_group(_report(tmp_path))
        assert group["kind"] == "needs_review"
        live, stale = group["rows"]
        assert (live["user_id"], live["cognito_user"]) == ("bbbb-new", "exists")
        assert (stale["user_id"], stale["cognito_user"]) == ("aaaa-old", "absent")
        assert stale["action"] == "skip: group needs review"
        assert users.get_item(Key={"PK": "USER#aaaa-old", "SK": "PROFILE"})["Item"]["status"] == "active"

    def test_markdown_report_lists_every_section(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["users"].put_item(Item=_profile_item("999", "gone@x.edu", "2026-04-01T00:00:00Z"))

        audit.run(_args(tmp_path), stack["clients"], "123", now=NOW)

        md = next(tmp_path.glob("*.md")).read_text()
        for heading in ("## Summary", "## Checks", "## Legacy pairs (1)", "## Needs review (0)",
                        "## Numeric rows with no twin (1)"):
            assert heading in md
        assert f"`{LEGACY}`" in md and "gone@x.edu" in md


class TestStillInUse:
    """lastLoginAt can't show API-key use, so the audit looks for it directly."""

    def _verdict(self, stack, tmp_path, *extra):
        audit.run(_args(tmp_path, *extra), stack["clients"], "123", now=NOW)
        return _only_group(_report(tmp_path))["rows"][1]

    def test_an_unexpired_api_key_is_in_use_even_if_unused_lately(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["api-keys"].put_item(Item={
            "PK": f"USER#{LEGACY}", "SK": "KEY#k1", "keyId": "k1", "name": "canvas sync", "userId": LEGACY,
            "expiresAt": "2027-03-01T00:00:00+00:00", "lastUsedAt": "2026-04-01T00:00:00Z",
        })

        stale = self._verdict(stack, tmp_path)

        assert stale["verdict"] == "in_use"
        assert "unexpired API key 'canvas sync'" in stale["in_use"][0]

    def test_a_key_with_no_expiry_is_in_use(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["api-keys"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "KEY#k1", "keyId": "k1"})

        assert self._verdict(stack, tmp_path)["verdict"] == "in_use"

    def test_a_model_call_after_the_cutover_is_in_use(self, stack, tmp_path):
        # An expired key, but the old id made a model call last week.
        _seed_pair(stack)
        stack["tables"]["sessions-metadata"].put_item(
            Item={"PK": f"USER#{LEGACY}", "SK": "C#2026-09-17T14:02:11.482913+00:00#abc"})

        stale = self._verdict(stack, tmp_path)

        assert stale["verdict"] == "in_use"
        assert stale["last_activity_at"] == "2026-09-17T14:02:11Z"
        assert "after this person's live profile was created (2026-04-16T09:00:00Z)" in stale["in_use"][0]

    def test_history_before_the_cutover_is_only_referenced(self, stack, tmp_path):
        _seed_pair(stack)
        t = stack["tables"]["sessions-metadata"]
        t.put_item(Item={"PK": f"USER#{LEGACY}", "SK": "C#2026-04-15T10:00:00Z#abc"})
        t.put_item(Item={"PK": f"USER#{LEGACY}", "SK": "S#s1", "lastMessageAt": "2026-04-15T10:00:05Z"})

        stale = self._verdict(stack, tmp_path)

        assert stale["verdict"] == "referenced"
        assert stale["activity"] == {"message": "2026-04-15T10:00:05Z", "model call": "2026-04-15T10:00:00Z"}

    def test_an_active_scheduled_prompt_is_in_use(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["sessions-metadata"].put_item(Item={
            "PK": f"USER#{LEGACY}", "SK": "SCHEDPROMPT#p1", "nextRunAt": "2026-09-25T08:00:00Z"})

        assert self._verdict(stack, tmp_path)["verdict"] == "in_use"

    def test_in_use_rows_are_never_marked_and_are_listed_first(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["api-keys"].put_item(Item={"PK": f"USER#{LEGACY}", "SK": "KEY#k1", "keyId": "k1"})

        stale = self._verdict(stack, tmp_path, "--apply", "mark", "--confirm-prefix", PREFIX)

        assert stale["action"] == "skip: verdict is in_use"
        item = stack["tables"]["users"].get_item(Key={"PK": f"USER#{LEGACY}", "SK": "PROFILE"})["Item"]
        assert item["status"] == "active" and "mergedInto" not in item
        md = next(tmp_path.glob("*.md")).read_text()
        assert md.index("## ⚠️ Old id still in use (1)") < md.index("## Legacy pairs")
        assert _report(tmp_path)["summary"]["inUse"] == 1


class TestApply:
    def _stale(self, stack):
        return stack["tables"]["users"].get_item(Key={"PK": f"USER#{LEGACY}", "SK": "PROFILE"}).get("Item")

    def test_mark_retires_the_row_reversibly(self, stack, tmp_path):
        _seed_pair(stack)

        audit.run(_args(tmp_path, "--apply", "mark", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW)

        item = self._stale(stack)
        assert item["status"] == "inactive" and item["GSI3PK"] == "STATUS#inactive"
        assert item["mergedInto"] == LIVE and item["mergedAt"] == "2026-09-24T12:00:00Z"
        assert item["lastLoginAt"] == "2026-04-15T00:00:00Z"
        assert _report(tmp_path)["summary"]["written"] == 1

    def test_marked_row_still_parses_and_the_api_still_resolves_the_live_row(self, stack, tmp_path):
        _seed_pair(stack)
        audit.run(_args(tmp_path, "--apply", "mark", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW)

        assert audit.item_to_profile(self._stale(stack)).status.value == "inactive"

    def test_mark_skips_a_row_that_signed_in_after_the_audit_read_it(self, stack):
        _seed_pair(stack)
        users = stack["tables"]["users"]
        [group], _, _ = audit.build_groups(audit.scan_profiles(users))
        group.rows[1].verdict = "unreferenced"
        users.update_item(Key={"PK": f"USER#{LEGACY}", "SK": "PROFILE"},
                          UpdateExpression="SET lastLoginAt = :t", ExpressionAttributeValues={":t": "2026-09-23T00:00:00Z"})

        written = audit.apply_actions(users, [group], "mark", now=NOW, min_soak_days=7)

        assert written == 0
        assert group.rows[1].action == "skip: signed in since the audit read it"
        assert "mergedInto" not in self._stale(stack)

    def test_delete_needs_a_prior_mark(self, stack, tmp_path):
        _seed_pair(stack)

        audit.run(_args(tmp_path, "--apply", "delete", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW)

        assert self._stale(stack) is not None
        assert _only_group(_report(tmp_path))["rows"][1]["action"].startswith("skip: not marked")

    def test_delete_waits_out_the_soak_then_deletes(self, stack, tmp_path):
        _seed_pair(stack)
        mark_dir, early_dir, late_dir = tmp_path / "mark", tmp_path / "early", tmp_path / "late"
        audit.run(_args(mark_dir, "--apply", "mark", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW)

        audit.run(_args(early_dir, "--apply", "delete", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW + timedelta(days=3))
        assert self._stale(stack) is not None
        assert "soak is 7d" in _only_group(_report(early_dir))["rows"][1]["action"]

        audit.run(_args(late_dir, "--apply", "delete", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW + timedelta(days=8))
        assert self._stale(stack) is None
        assert _only_group(_report(late_dir))["rows"][1]["action"] == "deleted"
        assert stack["tables"]["users"].get_item(Key={"PK": f"USER#{LIVE}", "SK": "PROFILE"}).get("Item")

    def test_delete_refuses_a_row_marked_for_a_different_live_id(self, stack, tmp_path):
        _seed_pair(stack)
        stack["tables"]["users"].update_item(
            Key={"PK": f"USER#{LEGACY}", "SK": "PROFILE"},
            UpdateExpression="SET mergedInto = :o, mergedAt = :t",
            ExpressionAttributeValues={":o": "someone-else", ":t": "2026-09-01T00:00:00Z"},
        )

        audit.run(_args(tmp_path, "--apply", "delete", "--confirm-prefix", PREFIX),
                  stack["clients"], "123", now=NOW)

        assert self._stale(stack) is not None


class TestCli:
    def test_apply_requires_matching_confirm_prefix(self):
        assert audit.main(["--project-prefix", PREFIX, "--region", REGION, "--apply", "mark"]) == 2
        assert audit.main(["--project-prefix", PREFIX, "--region", REGION, "--apply", "mark",
                           "--confirm-prefix", "boisestateai-v2"]) == 2

    def test_rejects_a_malformed_prefix(self):
        assert audit.main(["--project-prefix", "Bad_Prefix", "--region", REGION]) == 2


# ---------------------------------------------------------------------------
# Coverage guard: a new CDK table must be classified
# ---------------------------------------------------------------------------


def _cdk_table_suffixes():
    pattern = re.compile(r"getResourceName\(config,\s*'([^']+)'\)")
    found = set()
    for ts_file in (REPO_ROOT / "infrastructure" / "lib" / "constructs").rglob("*.ts"):
        if ts_file.name.endswith(".d.ts"):
            continue
        lines = ts_file.read_text().split("\n")
        for i, line in enumerate(lines):
            if "tableName:" in line or "new dynamodb.Table" in line:
                found.update(pattern.findall("\n".join(lines[max(0, i - 1): i + 5])))
    return found


class TestCoverage:
    def test_every_cdk_table_is_checked_or_explained(self):
        cdk = _cdk_table_suffixes()
        assert len(cdk) > 20, "CDK table scan found too little; did the constructs move?"
        classified = {s.logical for s in audit.TABLE_SOURCES} | set(audit.TABLES_WITHOUT_USER_IDS)
        missing = cdk - classified
        assert not missing, (
            f"CDK tables not classified in audit_user_duplicates.py: {sorted(missing)}. If they store a "
            "user id, add a TableSource + QueryCheck; otherwise add them to TABLES_WITHOUT_USER_IDS."
        )

    def test_no_phantom_tables(self):
        classified = {s.logical for s in audit.TABLE_SOURCES} | set(audit.TABLES_WITHOUT_USER_IDS)
        assert classified - _cdk_table_suffixes() == set()

    def test_every_check_and_deep_table_is_discoverable(self):
        logical = {s.logical for s in audit.TABLE_SOURCES}
        assert {c.table for c in audit.QUERY_CHECKS} <= logical
        assert set(audit.DEEP_SCAN_DEFAULT) <= logical
        assert len({c.check_id for c in audit.QUERY_CHECKS}) == len(audit.QUERY_CHECKS)
