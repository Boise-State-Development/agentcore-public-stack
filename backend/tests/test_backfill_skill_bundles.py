"""Tests for the skill-bundle backfill script (Skills v2 PR-3 follow-up).

The script's job is to make a v1 skill's S3 prefix a valid agentskills.io
bundle: a ``SKILL.md`` projection plus resources in the standard directory
layout, with the manifest pointing at the new keys.
"""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_skill_bundles import (  # noqa: E402
    is_legacy_key,
    migrate_resources,
    scan_skills,
    write_projection,
)

REGION = "us-east-1"
TABLE = "test-app-roles"
BUCKET = "test-skill-resources"

LEGACY_HASH = "1b6af35536890b2d21fbe1be58b2155d869f20a73acec0d16afeee71dda3fd92"
LEGACY_KEY = f"skills/web_research/{LEGACY_HASH}"
STANDARD_KEY = "skills/web_research/references/extraction_tips.md"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def table(aws):
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
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
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def s3(aws):
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


def _legacy_row():
    return {
        "PK": "SKILL#web_research",
        "SK": "METADATA",
        "skillId": "web_research",
        "displayName": "Web Research Assistant",
        "description": "Research a topic and produce citable notes.",
        "instructions": "# Web Research Assistant\n\nFetch, extract, cite.",
        "resources": [
            {
                "filename": "extraction_tips.md",
                "contentHash": LEGACY_HASH,
                "size": 824,
                "contentType": "text/markdown",
                "s3Key": LEGACY_KEY,
                # NB: no `kind` — v1 rows predate the field.
            }
        ],
    }


# =============================================================================
# is_legacy_key
# =============================================================================


def test_content_hash_keys_are_legacy():
    assert is_legacy_key(LEGACY_KEY, "web_research") is True


@pytest.mark.parametrize(
    "key",
    [
        "skills/web_research/references/extraction_tips.md",
        "skills/web_research/scripts/build.py",
        "skills/web_research/assets/logo.png",
    ],
)
def test_standard_layout_keys_are_not_legacy(key):
    assert is_legacy_key(key, "web_research") is False


def test_unrecognized_prefix_is_treated_as_legacy():
    """A key from another skill's prefix gets normalized rather than trusted."""
    assert is_legacy_key("skills/other/refs/x.md", "web_research") is True


# =============================================================================
# migrate_resources
# =============================================================================


def test_dry_run_touches_nothing(s3):
    s3.put_object(Bucket=BUCKET, Key=LEGACY_KEY, Body=b"# Tips")

    migrated, moved = migrate_resources(
        s3, BUCKET, "web_research", _legacy_row()["resources"],
        apply=False, delete_legacy=False,
    )

    assert moved == 1
    assert migrated[0]["s3Key"] == STANDARD_KEY  # what *would* be written
    # ...but S3 is untouched.
    listing = s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    assert [o["Key"] for o in listing] == [LEGACY_KEY]


def test_apply_copies_into_the_standard_layout(s3):
    s3.put_object(Bucket=BUCKET, Key=LEGACY_KEY, Body=b"# Tips")

    migrated, moved = migrate_resources(
        s3, BUCKET, "web_research", _legacy_row()["resources"],
        apply=True, delete_legacy=False,
    )

    assert moved == 1
    assert migrated[0]["s3Key"] == STANDARD_KEY
    assert migrated[0]["kind"] == "reference"
    assert s3.get_object(Bucket=BUCKET, Key=STANDARD_KEY)["Body"].read() == b"# Tips"
    # Legacy object survives by default — the copy is non-destructive.
    assert s3.head_object(Bucket=BUCKET, Key=LEGACY_KEY)


def test_delete_legacy_removes_the_old_object(s3):
    s3.put_object(Bucket=BUCKET, Key=LEGACY_KEY, Body=b"# Tips")

    migrate_resources(
        s3, BUCKET, "web_research", _legacy_row()["resources"],
        apply=True, delete_legacy=True,
    )

    assert s3.get_object(Bucket=BUCKET, Key=STANDARD_KEY)["Body"].read() == b"# Tips"
    with pytest.raises(s3.exceptions.ClientError):
        s3.head_object(Bucket=BUCKET, Key=LEGACY_KEY)


def test_missing_legacy_object_leaves_the_manifest_alone(s3):
    """Rewriting a key to bytes that don't exist would be a lie."""
    migrated, moved = migrate_resources(
        s3, BUCKET, "web_research", _legacy_row()["resources"],
        apply=True, delete_legacy=False,
    )

    assert moved == 0
    assert migrated[0]["s3Key"] == LEGACY_KEY


def test_is_idempotent(s3):
    s3.put_object(Bucket=BUCKET, Key=LEGACY_KEY, Body=b"# Tips")

    once, _ = migrate_resources(
        s3, BUCKET, "web_research", _legacy_row()["resources"],
        apply=True, delete_legacy=True,
    )
    twice, moved_again = migrate_resources(
        s3, BUCKET, "web_research", once, apply=True, delete_legacy=True,
    )

    assert moved_again == 0
    assert twice == once


# =============================================================================
# write_projection
# =============================================================================


def test_projection_is_a_valid_bundle_head(s3):
    write_projection(s3, BUCKET, _legacy_row(), apply=True)

    body = s3.get_object(Bucket=BUCKET, Key="skills/web_research/SKILL.md")[
        "Body"
    ].read().decode()

    assert body.startswith("---")
    assert "name: web-research" in body
    assert "Research a topic and produce citable notes." in body
    assert "Fetch, extract, cite." in body


def test_projection_carries_frontmatter_passthrough(s3):
    row = _legacy_row()
    row["allowedTools"] = ["fetch_url_content"]
    row["skillMetadata"] = {"license": "MIT"}

    write_projection(s3, BUCKET, row, apply=True)
    body = s3.get_object(Bucket=BUCKET, Key="skills/web_research/SKILL.md")[
        "Body"
    ].read().decode()

    assert "fetch_url_content" in body
    assert "license: MIT" in body


def test_projection_dry_run_writes_nothing(s3):
    write_projection(s3, BUCKET, _legacy_row(), apply=False)

    assert s3.list_objects_v2(Bucket=BUCKET).get("Contents", []) == []


# =============================================================================
# scan_skills
# =============================================================================


def test_scan_can_target_one_skill(table):
    table.put_item(Item=_legacy_row())
    table.put_item(
        Item={
            "PK": "SKILL#other",
            "SK": "METADATA",
            "skillId": "other",
            "description": "d",
            "instructions": "i",
        }
    )

    assert [r["skillId"] for r in scan_skills(table, "web_research")] == ["web_research"]
    assert len(scan_skills(table, None)) == 2


def test_scan_ignores_non_skill_rows(table):
    table.put_item(Item=_legacy_row())
    table.put_item(Item={"PK": "ROLE#admin", "SK": "METADATA", "roleId": "admin"})

    assert [r["skillId"] for r in scan_skills(table, None)] == ["web_research"]


def test_scan_returns_empty_for_a_missing_skill(table):
    assert scan_skills(table, "nope") == []
