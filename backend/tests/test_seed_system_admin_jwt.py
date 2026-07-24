"""Tests for seed_system_admin_role and seed_default_tools in seed_bootstrap_data.py."""

import sys
import os
import pytest
import boto3
from moto import mock_aws

# Add the scripts directory to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from seed_bootstrap_data import (  # noqa: E402
    EXAMPLE_SKILL_ID,
    seed_default_role,
    seed_example_skills,
    seed_system_admin_role,
    seed_default_tools,
)

TABLE_NAME = "test-app-roles"
REGION = "us-east-1"


@pytest.fixture
def dynamodb_table():
    """Create a mock DynamoDB table matching the app-roles schema."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "JwtRoleMappingIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
        yield table


class TestSeedSystemAdminRole:
    def test_creates_role_with_grants(self, dynamodb_table):
        """Creates DEFINITION + TOOL_GRANT#* + MODEL_GRANT#* + JWT_MAPPING#system_admin."""
        result = seed_system_admin_role(TABLE_NAME, REGION)

        assert result.created == 1
        assert result.failed == 0

        # Verify DEFINITION
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "DEFINITION"}
        )
        item = resp["Item"]
        assert item["roleId"] == "system_admin"
        assert item["jwtRoleMappings"] == ["system_admin"]
        assert item["grantedTools"] == ["*"]
        assert item["grantedModels"] == ["*"]
        assert item["grantedSkills"] == ["*"]
        assert item["effectivePermissions"]["skills"] == ["*"]
        assert item["isSystemRole"] is True
        assert item["priority"] == 1000

        # Verify TOOL_GRANT#*
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "TOOL_GRANT#*"}
        )
        grant = resp["Item"]
        assert grant["GSI2PK"] == "TOOL#*"
        assert grant["GSI2SK"] == "ROLE#system_admin"
        assert grant["enabled"] is True

        # Verify MODEL_GRANT#*
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "MODEL_GRANT#*"}
        )
        grant = resp["Item"]
        assert grant["GSI3PK"] == "MODEL#*"
        assert grant["GSI3SK"] == "ROLE#system_admin"
        assert grant["enabled"] is True

        # Verify SKILL_GRANT#* — reuses the GSI2 keyspace with a SKILL# partition
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "SKILL_GRANT#*"}
        )
        grant = resp["Item"]
        assert grant["GSI2PK"] == "SKILL#*"
        assert grant["GSI2SK"] == "ROLE#system_admin"
        assert grant["enabled"] is True

        # Verify JWT_MAPPING#system_admin (maps Cognito group → AppRole)
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "JWT_MAPPING#system_admin"}
        )
        mapping = resp["Item"]
        assert mapping["GSI1PK"] == "JWT_ROLE#system_admin"
        assert mapping["GSI1SK"] == "ROLE#system_admin"
        assert mapping["roleId"] == "system_admin"
        assert mapping["enabled"] is True

    def test_skips_when_role_exists(self, dynamodb_table):
        """Skips if system_admin DEFINITION already present."""
        seed_system_admin_role(TABLE_NAME, REGION)

        result = seed_system_admin_role(TABLE_NAME, REGION)

        assert result.skipped == 1
        assert result.created == 0


class TestSeedDefaultTools:
    def test_creates_default_tools(self, dynamodb_table):
        """Creates the default tool entries."""
        result = seed_default_tools(TABLE_NAME, REGION)

        assert result.created == 9
        assert result.failed == 0

        # Verify fetch_url_content
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#fetch_url_content", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "fetch_url_content"
        assert item["displayName"] == "URL Fetcher"
        assert item["category"] == "search"
        assert item["protocol"] == "local"
        assert item["status"] == "active"
        assert item["enabledByDefault"] is True
        assert item["isPublic"] is False
        assert item["GSI1PK"] == "CATEGORY#search"
        assert item["GSI1SK"] == "TOOL#fetch_url_content"

        # Verify create_visualization
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#create_visualization", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "create_visualization"
        assert item["displayName"] == "Charts & Graphs"
        assert item["category"] == "data"
        assert item["enabledByDefault"] is False
        assert item["GSI1PK"] == "CATEGORY#data"
        assert item["GSI1SK"] == "TOOL#create_visualization"

        # Verify calculator
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#calculator", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "calculator"
        assert item["displayName"] == "Calculator"
        assert item["category"] == "utility"
        assert item["protocol"] == "local"

        # Verify generate_diagram_and_validate
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#generate_diagram_and_validate", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "generate_diagram_and_validate"
        assert item["displayName"] == "Code Interpreter"
        assert item["category"] == "code"
        assert item["protocol"] == "local"

        # Verify create_artifact
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#create_artifact", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "create_artifact"
        assert item["displayName"] == "Artifacts"
        assert item["category"] == "document"
        assert item["protocol"] == "local"
        assert item["enabledByDefault"] is True
        assert item["isPublic"] is True
        assert item["GSI1PK"] == "CATEGORY#document"
        assert item["GSI1SK"] == "TOOL#create_artifact"

        # update_artifact is no longer its own catalog row: create_artifact is
        # the single gate key that provisions the whole artifact toolset.
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#update_artifact", "SK": "METADATA"}
        )
        assert "Item" not in resp

        # Verify create_word_document (single toggle for the whole Word toolset)
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#create_word_document", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "create_word_document"
        assert item["displayName"] == "Word Documents"
        assert item["category"] == "document"
        assert item["protocol"] == "local"
        assert item["enabledByDefault"] is False
        assert item["isPublic"] is True
        assert item["GSI1PK"] == "CATEGORY#document"
        assert item["GSI1SK"] == "TOOL#create_word_document"

        # Verify workspace_files (single toggle for the workspace toolset)
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#workspace_files", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "workspace_files"
        assert item["displayName"] == "File Workspace"
        assert item["category"] == "document"
        assert item["protocol"] == "local"
        assert item["enabledByDefault"] is False
        assert item["isPublic"] is True
        assert item["GSI1PK"] == "CATEGORY#document"
        assert item["GSI1SK"] == "TOOL#workspace_files"

        # Verify create_excel_spreadsheet (single toggle for the whole Excel toolset)
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#create_excel_spreadsheet", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "create_excel_spreadsheet"
        assert item["displayName"] == "Excel Spreadsheets"
        assert item["category"] == "document"
        assert item["protocol"] == "local"
        assert item["enabledByDefault"] is False
        assert item["isPublic"] is True
        assert item["GSI1PK"] == "CATEGORY#document"
        assert item["GSI1SK"] == "TOOL#create_excel_spreadsheet"

        # Verify create_powerpoint_presentation (single toggle for the whole PowerPoint toolset)
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#create_powerpoint_presentation", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "create_powerpoint_presentation"
        assert item["displayName"] == "PowerPoint Presentations"
        assert item["category"] == "document"
        assert item["protocol"] == "local"
        assert item["enabledByDefault"] is False
        assert item["isPublic"] is True
        assert item["GSI1PK"] == "CATEGORY#document"
        assert item["GSI1SK"] == "TOOL#create_powerpoint_presentation"

    def test_skips_existing_tools(self, dynamodb_table):
        """Skips tools that already exist."""
        seed_default_tools(TABLE_NAME, REGION)

        result = seed_default_tools(TABLE_NAME, REGION)

        assert result.skipped == 9
        assert result.created == 0

    def test_partial_skip(self, dynamodb_table):
        """Skips only the tool that already exists, creates the rest."""
        # Pre-create one tool
        dynamodb_table.put_item(Item={
            "PK": "TOOL#fetch_url_content",
            "SK": "METADATA",
            "toolId": "fetch_url_content",
        })

        result = seed_default_tools(TABLE_NAME, REGION)

        assert result.created == 8
        assert result.skipped == 1


class TestSeedExampleSkills:
    """seed_example_skills — the PR-6b bundled-skill seed."""

    def test_creates_skill_and_grants_to_default(self, dynamodb_table, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        seed_default_role(TABLE_NAME, REGION)

        result = seed_example_skills(TABLE_NAME, REGION)
        assert result.created == 1
        assert result.failed == 0

        # SKILL# record — instructions (knowledge bundle) + active.
        item = dynamodb_table.get_item(
            Key={"PK": f"SKILL#{EXAMPLE_SKILL_ID}", "SK": "METADATA"}
        )["Item"]
        assert item["skillId"] == EXAMPLE_SKILL_ID
        assert item["status"] == "active"
        assert "boundToolIds" not in item
        assert item["instructions"]
        assert item["GSI4PK"] == "OWNER#system"
        # No bucket configured → seeded without reference bytes.
        assert item["resources"] == []

        # Default role granted (effectivePermissions is what RBAC reads).
        role = dynamodb_table.get_item(
            Key={"PK": "ROLE#default", "SK": "DEFINITION"}
        )["Item"]
        assert EXAMPLE_SKILL_ID in role["grantedSkills"]
        assert EXAMPLE_SKILL_ID in role["effectivePermissions"]["skills"]

        # Reverse-lookup grant item (GSI2 SKILL# keyspace).
        grant = dynamodb_table.get_item(
            Key={"PK": "ROLE#default", "SK": f"SKILL_GRANT#{EXAMPLE_SKILL_ID}"}
        )["Item"]
        assert grant["GSI2PK"] == f"SKILL#{EXAMPLE_SKILL_ID}"
        assert grant["GSI2SK"] == "ROLE#default"
        assert grant["enabled"] is True

    def test_idempotent_skip(self, dynamodb_table, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        seed_default_role(TABLE_NAME, REGION)
        seed_example_skills(TABLE_NAME, REGION)

        result = seed_example_skills(TABLE_NAME, REGION)
        assert result.skipped == 1
        assert result.created == 0

    def test_grant_skipped_when_no_default_role(self, dynamodb_table, monkeypatch):
        monkeypatch.delenv("S3_SKILL_RESOURCES_BUCKET_NAME", raising=False)
        # No default role seeded — skill still created, grant no-ops.
        result = seed_example_skills(TABLE_NAME, REGION)
        assert result.created == 1
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#default", "SK": f"SKILL_GRANT#{EXAMPLE_SKILL_ID}"}
        )
        assert "Item" not in resp

    def test_uploads_reference_file_to_s3_when_bucket_set(
        self, dynamodb_table, monkeypatch
    ):
        bucket = "test-skill-resources"
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        monkeypatch.setenv("S3_SKILL_RESOURCES_BUCKET_NAME", bucket)
        seed_default_role(TABLE_NAME, REGION)

        seed_example_skills(TABLE_NAME, REGION)

        item = dynamodb_table.get_item(
            Key={"PK": f"SKILL#{EXAMPLE_SKILL_ID}", "SK": "METADATA"}
        )["Item"]
        assert len(item["resources"]) == 1
        ref = item["resources"][0]
        assert ref["filename"] == "extraction_tips.md"
        # Standard agentskills.io bundle layout, not the v1 content-hash key —
        # a seeded prefix must be a portable bundle on day one (Skills v2).
        assert ref["s3Key"] == f"skills/{EXAMPLE_SKILL_ID}/references/extraction_tips.md"
        assert ref["kind"] == "reference"
        body = s3.get_object(Bucket=bucket, Key=ref["s3Key"])["Body"].read()
        assert b"Extraction Tips" in body

    def test_writes_the_skill_md_projection(self, dynamodb_table, monkeypatch):
        """The seeded prefix must be a valid bundle, not just loose bytes.

        Without SKILL.md the prefix cannot be handed to a managed Harness
        (``{"s3": {"uri": ...}}``) or exported as-is.
        """
        bucket = "test-skill-resources"
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        monkeypatch.setenv("S3_SKILL_RESOURCES_BUCKET_NAME", bucket)
        seed_default_role(TABLE_NAME, REGION)

        seed_example_skills(TABLE_NAME, REGION)

        body = s3.get_object(
            Bucket=bucket, Key=f"skills/{EXAMPLE_SKILL_ID}/SKILL.md"
        )["Body"].read().decode()

        assert body.startswith("---")
        assert "name: web-research" in body
        assert "Fetch web pages and turn them into accurate, citable notes." in body
        assert "# Web Research Assistant" in body

    def test_seeded_instructions_name_no_retired_v1_tools(self):
        """The v1 disclosure meta-tools were deleted in Skills v2 PR-1/PR-2.

        Seed prose naming them would tell the model to call tools that no
        longer exist — the exact drift that stranded dev's row.
        """
        from seed_bootstrap_data import EXAMPLE_SKILL_INSTRUCTIONS

        assert "skill_executor" not in EXAMPLE_SKILL_INSTRUCTIONS
        assert "skill_dispatcher" not in EXAMPLE_SKILL_INSTRUCTIONS
