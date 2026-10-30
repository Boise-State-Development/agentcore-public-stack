"""The seeded web_research skill: its catalog line, and refreshing an untouched row.

The description is the only thing a model sees before deciding whether to load
a skill, so it is content worth pinning. A re-run of the bootstrap seed must
bring an untouched seeded row up to the current text, because the seed is the
only path that reaches existing deployments, and must never overwrite an
admin's edit.
"""

from __future__ import annotations

import os
import sys

import boto3
import pytest
import yaml
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_bootstrap_data as seed  # noqa: E402
from apis.shared.skills.models import SKILL_DESCRIPTION_MAX_LENGTH, SkillDefinition  # noqa: E402

REGION = "us-east-1"
TABLE = "test-app-roles"
BUCKET = "test-skill-resources"
OLD = seed.PREVIOUS_EXAMPLE_SKILL_DESCRIPTIONS[0]


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "S3_SKILL_RESOURCES_BUCKET_NAME": BUCKET,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _row(table):
    return table.get_item(Key={"PK": "SKILL#web_research", "SK": "METADATA"}).get("Item")


def _skill_md_description() -> str:
    body = boto3.client("s3", region_name=REGION).get_object(
        Bucket=BUCKET, Key="skills/web_research/SKILL.md"
    )["Body"].read().decode("utf-8")
    _, frontmatter, _ = body.split("---\n", 2)
    return yaml.safe_load(frontmatter)["description"]


def _age_row(table, description: str = OLD, **extra):
    """Make the seeded row look like one an older seed wrote."""
    names = {"#d": "description", "#u": "updatedAt"}
    values = {":d": description, ":u": "2026-01-01T00:00:00Z"}
    sets = ["#d = :d", "#u = :u"]
    for i, (key, value) in enumerate(extra.items()):
        names[f"#e{i}"] = key
        values[f":e{i}"] = value
        sets.append(f"#e{i} = :e{i}")
    table.update_item(
        Key={"PK": "SKILL#web_research", "SK": "METADATA"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


class TestDescription:
    def test_fits_the_catalog_and_frontmatter_limits(self):
        description = seed.EXAMPLE_SKILL_DESCRIPTION
        assert 0 < len(description) <= SKILL_DESCRIPTION_MAX_LENGTH
        assert "\n" not in description
        assert ": " not in description  # the My Skills importer splits on it

    def test_says_when_to_load_it_not_only_what_it_does(self):
        description = seed.EXAMPLE_SKILL_DESCRIPTION.lower()
        assert "use this whenever" in description
        assert "url" in description and "citation" in description

    def test_the_current_text_is_not_also_a_previous_one(self):
        assert seed.EXAMPLE_SKILL_DESCRIPTION not in seed.PREVIOUS_EXAMPLE_SKILL_DESCRIPTIONS


class TestSeed:
    def test_a_fresh_seed_writes_the_current_description_to_row_and_skill_md(self, aws):
        result = seed.seed_example_skills(TABLE, REGION)
        assert (result.created, result.failed) == (1, 0), result.details
        skill = SkillDefinition.from_dynamo_item(_row(aws))
        assert skill.description == seed.EXAMPLE_SKILL_DESCRIPTION
        assert _skill_md_description() == seed.EXAMPLE_SKILL_DESCRIPTION

    def test_a_rerun_refreshes_an_untouched_row_and_bumps_updated_at(self, aws):
        seed.seed_example_skills(TABLE, REGION)
        _age_row(aws)

        result = seed.seed_example_skills(TABLE, REGION)

        assert (result.created, result.updated, result.skipped, result.failed) == (0, 1, 0, 0), result.details
        row = _row(aws)
        assert row["description"] == seed.EXAMPLE_SKILL_DESCRIPTION
        assert row["updatedAt"] > "2026-01-01T00:00:00Z"  # the agent cache keys on it
        assert _skill_md_description() == seed.EXAMPLE_SKILL_DESCRIPTION

    def test_the_refresh_keeps_an_admins_edited_instructions(self, aws):
        seed.seed_example_skills(TABLE, REGION)
        _age_row(aws, instructions="# Edited by an admin\n\nDo it this way.")
        seed.seed_example_skills(TABLE, REGION)
        body = boto3.client("s3", region_name=REGION).get_object(
            Bucket=BUCKET, Key="skills/web_research/SKILL.md"
        )["Body"].read().decode("utf-8")
        assert "Edited by an admin" in body
        assert _row(aws)["instructions"].startswith("# Edited by an admin")

    def test_an_admins_description_is_left_alone(self, aws):
        seed.seed_example_skills(TABLE, REGION)
        _age_row(aws, description="Our own research helper.")
        result = seed.seed_example_skills(TABLE, REGION)
        assert (result.updated, result.skipped) == (0, 1)
        assert _row(aws)["description"] == "Our own research helper."

    def test_a_current_row_is_skipped_not_rewritten(self, aws):
        seed.seed_example_skills(TABLE, REGION)
        before = _row(aws)["updatedAt"]
        result = seed.seed_example_skills(TABLE, REGION)
        assert (result.updated, result.skipped) == (0, 1)
        assert _row(aws)["updatedAt"] == before

    def test_a_users_skill_holding_the_id_is_never_touched(self, aws):
        aws.put_item(Item={
            "PK": "SKILL#web_research", "SK": "METADATA", "ownerId": "user-1", "description": OLD,
        })
        result = seed.seed_example_skills(TABLE, REGION)
        assert (result.updated, result.skipped) == (0, 1)
        assert _row(aws)["ownerId"] == "user-1"
