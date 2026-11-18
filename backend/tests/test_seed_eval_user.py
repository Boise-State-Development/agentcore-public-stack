"""``scripts/seed_eval_user.py`` — the eval harness's own user, role and credentials.

Pins what the harness depends on: the role grants exactly what the golden set
offers (so a new case that needs a tool fails here, not on release day), the
password satisfies the pool policy and is stored only as a SecureString, the
Cognito user is confirmed and in the role's group, a re-run reuses the stored
password instead of rotating it, and the user's long-term memory is off.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_eval_user as seed  # noqa: E402

REGION = "us-east-1"
PREFIX = "test-stack"


class TestRole:
    def test_grants_exactly_what_the_golden_set_offers(self):
        grants = seed.grants_from_golden_set()
        role = seed.build_role(tools=grants["tools"], skills=grants["skills"], models=["*"])
        assert role.role_id == "eval_harness"
        assert role.jwt_role_mappings == [seed.GROUP]
        assert role.granted_tools == grants["tools"] and "calculator" in role.granted_tools
        assert role.granted_skills == ["skill_creator", "web_research"]
        assert role.granted_models == ["*"]
        # The half the access checks read; a role with only granted_* grants nothing.
        assert role.effective_permissions.tools == role.granted_tools
        assert role.effective_permissions.skills == role.granted_skills
        assert role.effective_permissions.models == ["*"]

    def test_dry_run_needs_no_prefix(self):
        assert seed.parse_args(["--dry-run"]).dry_run
        with pytest.raises(SystemExit):
            seed.parse_args([])


def test_generated_passwords_meet_every_cognito_class():
    for _ in range(50):
        pw = seed.generate_password()
        assert len(pw) == 32
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        yield


class TestCredentials:
    def test_created_once_as_a_secure_string_then_reused(self, aws):
        ssm = boto3.client("ssm", region_name=REGION)
        first = seed.ensure_credentials(ssm, PREFIX)
        assert first["username"] == seed.USERNAME
        param = ssm.get_parameter(Name=seed.credentials_parameter(PREFIX))["Parameter"]
        assert param["Type"] == "SecureString"

        second = seed.ensure_credentials(ssm, PREFIX)
        assert second == first

    def test_a_malformed_parameter_is_replaced(self, aws):
        ssm = boto3.client("ssm", region_name=REGION)
        ssm.put_parameter(Name=seed.credentials_parameter(PREFIX), Value=json.dumps({"username": "someone-else"}),
                          Type="SecureString")
        creds = seed.ensure_credentials(ssm, PREFIX)
        assert creds["username"] == seed.USERNAME and creds["password"]


class TestCognitoUser:
    def _pool(self):
        idp = boto3.client("cognito-idp", region_name=REGION)
        pool = idp.create_user_pool(PoolName="p", AliasAttributes=["email"])["UserPool"]["Id"]
        return idp, pool

    def test_creates_a_confirmed_user_in_the_group(self, aws):
        idp, pool = self._pool()
        sub = seed.ensure_cognito_user(idp, pool, seed.generate_password())

        user = idp.admin_get_user(UserPoolId=pool, Username=seed.USERNAME)
        attrs = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
        assert user["UserStatus"] == "CONFIRMED"
        assert attrs["sub"] == sub and attrs["email"] == seed.EMAIL
        groups = idp.admin_list_groups_for_user(UserPoolId=pool, Username=seed.USERNAME)["Groups"]
        assert [g["GroupName"] for g in groups] == [seed.GROUP]

    def test_a_rerun_is_idempotent(self, aws):
        idp, pool = self._pool()
        pw = seed.generate_password()
        first = seed.ensure_cognito_user(idp, pool, pw)
        assert seed.ensure_cognito_user(idp, pool, pw) == first
        assert len(idp.list_users(UserPoolId=pool)["Users"]) == 1


class TestRepositories:
    def test_role_upsert_creates_then_updates(self, aws):
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName="roles",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "JwtRoleMappingIndex",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        from apis.shared.rbac.repository import AppRoleRepository

        repo = AppRoleRepository(table_name="roles")
        role = seed.build_role(tools=["calculator"], skills=["web_research"], models=["*"])
        assert asyncio.run(seed.upsert_role(role, repo)) == "created"
        assert asyncio.run(seed.upsert_role(role, repo)) == "updated"
        stored = asyncio.run(repo.get_role("eval_harness"))
        assert stored.effective_permissions.skills == ["web_research"]

    def test_memory_is_turned_off_and_other_settings_survive(self, aws):
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName="settings",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        from apis.shared.user_settings.models import memory_mode_from
        from apis.shared.user_settings.repository import UserSettingsRepository

        repo = UserSettingsRepository(table_name="settings")
        asyncio.run(repo.update_settings("user-1", {"personalInstructions": "be brief"}))
        asyncio.run(seed.turn_memory_off("user-1", repo))
        settings = asyncio.run(repo.get_settings("user-1"))
        assert memory_mode_from(settings) == "off"
        assert settings["personalInstructions"] == "be brief"


class TestQuota:
    """The eval users' standing quota: one tier, assigned to their app role, idempotent.

    Before it, the eval user lived on the $10 default tier plus hand-made
    overrides and extensions; on dev those ran out on 2026-11-15 and the
    nightly eval and smoke failed for three nights.
    """

    TABLE = "test-stack-user-quotas"

    @pytest.fixture
    def repo(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("AWS_REGION", REGION)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.setenv(key, "testing")
        with mock_aws():
            gsis = [("AssignmentTypeIndex", "GSI1PK", "GSI1SK"), ("AppRoleAssignmentIndex", "GSI6PK", "GSI6SK")]
            attrs = {"PK", "SK"} | {a for _, pk, sk in gsis for a in (pk, sk)}
            boto3.client("dynamodb").create_table(
                TableName=self.TABLE,
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {"IndexName": n, "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"},
                                                   {"AttributeName": sk, "KeyType": "RANGE"}],
                     "Projection": {"ProjectionType": "ALL"}}
                    for n, pk, sk in gsis
                ],
            )
            from agents.main_agent.quota.repository import QuotaRepository

            yield QuotaRepository(table_name=self.TABLE, events_table_name="test-stack-quota-events")

    def test_creates_the_tier_and_the_role_assignment_once(self, repo):
        now = "2026-11-18T00:00:00Z"
        assert asyncio.run(seed.ensure_quota(repo, seed.DEFAULT_MONTHLY_LIMIT, now=now)) == "created"
        assert asyncio.run(seed.ensure_quota(repo, seed.DEFAULT_MONTHLY_LIMIT, now=now)) == "current"

        tier = asyncio.run(repo.get_tier(seed.QUOTA_TIER_ID))
        assert tier.monthly_cost_limit == seed.DEFAULT_MONTHLY_LIMIT == 300
        assert tier.enabled and tier.action_on_limit == "block"
        # What the resolver reads for a user holding the eval_harness role.
        [assignment] = asyncio.run(repo.query_app_role_assignments(seed.ROLE_ID))
        assert assignment.tier_id == seed.QUOTA_TIER_ID and assignment.enabled

    def test_a_new_limit_updates_the_same_tier(self, repo):
        now = "2026-11-18T00:00:00Z"
        asyncio.run(seed.ensure_quota(repo, seed.DEFAULT_MONTHLY_LIMIT, now=now))
        from decimal import Decimal

        assert asyncio.run(seed.ensure_quota(repo, Decimal("450"), now=now)) == "updated"
        assert asyncio.run(repo.get_tier(seed.QUOTA_TIER_ID)).monthly_cost_limit == 450
        assert len(asyncio.run(repo.query_app_role_assignments(seed.ROLE_ID))) == 1

    def test_a_disabled_assignment_is_restored_with_its_role_index(self, repo):
        now = "2026-11-18T00:00:00Z"
        asyncio.run(seed.ensure_quota(repo, seed.DEFAULT_MONTHLY_LIMIT, now=now))
        asyncio.run(repo.update_assignment(seed.QUOTA_ASSIGNMENT_ID, {"enabled": False}))

        assert asyncio.run(seed.ensure_quota(repo, seed.DEFAULT_MONTHLY_LIMIT, now=now)) == "updated"
        [assignment] = asyncio.run(repo.query_app_role_assignments(seed.ROLE_ID))
        assert assignment.enabled

    def test_monthly_limit_flag(self):
        assert seed.parse_args(["--prefix", PREFIX]).monthly_limit == 300
        assert seed.parse_args(["--prefix", PREFIX, "--monthly-limit", "120"]).monthly_limit == 120
