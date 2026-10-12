"""The Skill Creator platform skill and its seed (docs/specs/skill-creator.md, PR-C).

Two halves. The **content** checks keep the Markdown honest against the code it
describes: every tool it names exists, every reference path it names is in the
bundle, its limits match the service's, and nothing from upstream's eval harness
leaked back in (D2). The **seed** checks pin how it lands in the catalog: an
admin-tier row granted to no role (the pilot role is chosen in the roles UI),
never overwriting an existing row, and never touching a user's skill that
happens to hold the id.
"""

import os
import re
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_bootstrap_data as seed  # noqa: E402
from agents.builtin_tools.skill_authoring.tools import build_skill_authoring_tools  # noqa: E402
from apis.shared.skills.bundle import generate_skill_md, slugify_skill_name  # noqa: E402
from apis.shared.skills.bundle_files import (  # noqa: E402
    MAX_RESOURCE_BYTES,
    MAX_RESOURCES_PER_SKILL,
    SkillBundleFiles,
)
from apis.shared.skills.models import SKILL_DESCRIPTION_MAX_LENGTH, SkillDefinition  # noqa: E402
from apis.shared.skills.resource_types import (  # noqa: E402
    is_text_content_type,
    resolve_upload_content_type,
)
from apis.shared.skills.user_service import MAX_SKILLS_PER_USER  # noqa: E402

BUNDLE_DIR = seed.SEED_SKILLS_DIR / seed.SKILL_CREATOR_ID
REGION = "us-east-1"
TABLE = "test-app-roles"
BUCKET = "test-skill-resources"


@pytest.fixture(scope="module")
def bundle():
    return seed.load_seed_skill_bundle(BUNDLE_DIR)


def _guidance_text(bundle) -> str:
    """The text the model is told to follow (not the license or notice)."""
    parts = [bundle.instructions]
    for name, content, _ in bundle.references:
        if name not in ("LICENSE.txt", "NOTICE.md"):
            parts.append(content.decode("utf-8"))
    return "\n".join(parts)


class TestContent:
    def test_frontmatter_names_the_runtime_slug_and_fits_the_limits(self, bundle):
        fm = bundle.frontmatter
        assert fm["name"] == slugify_skill_name(seed.SKILL_CREATOR_ID) == "skill-creator"
        assert 0 < len(fm["description"]) <= SKILL_DESCRIPTION_MAX_LENGTH
        assert "\n" not in fm["description"]
        assert fm["license"] == "Apache-2.0"

    def test_frontmatter_is_importable_by_the_spa(self, bundle):
        """The My Skills importer reads simple `key: value` lines, and the
        skill tells users to keep descriptions free of ': ' for that reason."""
        for key, value in bundle.frontmatter.items():
            assert ": " not in value, key

    def test_every_tool_it_names_exists(self, bundle):
        """Drift guard: renaming a my_skill_* tool must fail here, not leave the
        skill telling the model to call something that is not there."""
        real = {t.tool_name for t in build_skill_authoring_tools("s", "u", None)}
        named = set(re.findall(r"\bmy_skill_[a-z_]+\b", _guidance_text(bundle)))
        assert named, "the skill should drive the authoring tools"
        assert named <= real, named - real

    def test_every_reference_path_it_names_is_in_the_bundle(self, bundle):
        files = {name for name, _, _ in bundle.references}
        text = _guidance_text(bundle) + "\n".join(
            c.decode("utf-8") for n, c, _ in bundle.references if n == "NOTICE.md"
        )
        named = set(re.findall(r"references/([A-Za-z0-9._-]+)", text))
        # Examples of a *user's* skill paths, not files of this bundle.
        named -= {"forms.md", "<filename>"}
        assert named <= files, named - files

    def test_reference_files_are_valid_uploads(self, bundle):
        assert len(bundle.references) <= MAX_RESOURCES_PER_SKILL
        for name, content, _ in bundle.references:
            SkillBundleFiles.validate_filename(name)
            assert is_text_content_type(resolve_upload_content_type(name)), name
            assert 0 < len(content) <= MAX_RESOURCE_BYTES, name

    def test_stated_limits_match_the_code(self, bundle):
        text = bundle.instructions
        assert f"{MAX_SKILLS_PER_USER} skills per person" in text
        assert f"{MAX_RESOURCES_PER_SKILL} files per skill" in text
        assert f"{MAX_RESOURCE_BYTES // (1024 * 1024)} MiB per file" in text
        assert f"{SKILL_DESCRIPTION_MAX_LENGTH:,} characters" in text

    def test_it_says_where_to_turn_a_skill_on(self, bundle):
        """Spec D6: skills are opt-in per turn and a new one starts off. The
        slash command only appears once a skill is on."""
        assert "Customize → Skills" in bundle.instructions
        assert "new conversation" in bundle.instructions

    @pytest.mark.parametrize(
        "upstream_only",
        ["evals.json", "run_loop", "generate_review", "aggregate_benchmark", "claude -p", "subagent", "package_skill"],
    )
    def test_upstreams_eval_harness_did_not_leak_in(self, bundle, upstream_only):
        """D2: these are impossible in this runtime; a skill that narrates them
        reads as broken."""
        assert upstream_only not in _guidance_text(bundle)

    def test_it_carries_the_apache_notice(self, bundle):
        files = {name: content.decode("utf-8") for name, content, _ in bundle.references}
        assert "Apache License" in files["LICENSE.txt"]
        assert "Anthropic" in files["NOTICE.md"] and "Changes from the original" in files["NOTICE.md"]
        assert "references/NOTICE.md" in bundle.instructions

    def test_the_instructions_stay_lean(self, bundle):
        """Instructions load on every activation; detail goes to references."""
        assert len(bundle.instructions.splitlines()) < 120


class TestFrontmatterWriter:
    @pytest.mark.parametrize(
        "value",
        ["plain words", "Review: grants", "- leading dash", "has # hash", "\"quoted\"", "ünïcode: yes"],
    )
    def test_any_description_round_trips_through_yaml(self, value):
        line = f"description: {seed._frontmatter_value(value)}"
        assert yaml.safe_load(line) == {"description": value}


@pytest.fixture()
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
    return table.get_item(Key={"PK": "SKILL#skill_creator", "SK": "METADATA"}).get("Item")


class TestSeed:
    def test_creates_an_admin_tier_row_that_the_model_reads(self, aws, bundle):
        result = seed.seed_skill_creator(TABLE, REGION)
        assert (result.created, result.failed) == (1, 0), result.details

        item = _row(aws)
        skill = SkillDefinition.from_dynamo_item(item)
        assert skill.owner_id == "system"
        assert getattr(skill.visibility, "value", skill.visibility) == "admin"
        assert getattr(skill.status, "value", skill.status) == "active"
        assert skill.display_name == "Skill Creator"
        assert skill.description == bundle.frontmatter["description"]
        assert skill.instructions == bundle.instructions
        assert skill.skill_metadata == {"license": "Apache-2.0"}
        assert sorted(r.filename for r in skill.resources) == sorted(n for n, _, _ in bundle.references)
        assert all(r.s3_key == f"skills/skill_creator/references/{r.filename}" for r in skill.resources)
        assert item["GSI4PK"] == "OWNER#system"

    def test_grants_it_to_no_role(self, aws):
        """The pilot role is a deployment's choice (Admin → Roles); granting
        `default` here would be the wider rollout the spec calls PR-D."""
        seed.seed_skill_creator(TABLE, REGION)
        grants = [
            i for i in aws.scan()["Items"]
            if i["PK"].startswith("ROLE#") or "SKILL_GRANT#skill_creator" in i["SK"]
        ]
        assert grants == []

    def test_writes_a_skill_md_equivalent_to_the_apps_projection(self, aws, bundle):
        seed.seed_skill_creator(TABLE, REGION)
        written = boto3.client("s3", region_name=REGION).get_object(
            Bucket=BUCKET, Key="skills/skill_creator/SKILL.md"
        )["Body"].read().decode("utf-8")
        app = generate_skill_md(
            skill_id="skill_creator",
            description=bundle.frontmatter["description"],
            instructions=bundle.instructions,
            skill_metadata={"license": "Apache-2.0"},
        )

        def split(text):
            _, fm, body = text.split("---\n", 2)
            return yaml.safe_load(fm), body.strip()

        assert split(written) == split(app)

    def test_a_rerun_leaves_an_existing_row_alone(self, aws):
        seed.seed_skill_creator(TABLE, REGION)
        aws.update_item(
            Key={"PK": "SKILL#skill_creator", "SK": "METADATA"},
            UpdateExpression="SET description = :d",
            ExpressionAttributeValues={":d": "edited by an admin"},
        )
        result = seed.seed_skill_creator(TABLE, REGION)
        assert (result.created, result.skipped, result.failed) == (0, 1, 0)
        assert _row(aws)["description"] == "edited by an admin"

    def test_never_overwrites_a_users_skill_holding_the_id(self, aws):
        """Ids are global across the two tiers, so a user could own this one."""
        aws.put_item(Item={"PK": "SKILL#skill_creator", "SK": "METADATA", "ownerId": "user-1", "displayName": "Mine"})
        result = seed.seed_skill_creator(TABLE, REGION)
        assert result.failed == 1 and result.created == 0
        assert "belongs to a user's own skill" in result.details[0]
        assert _row(aws) == {"PK": "SKILL#skill_creator", "SK": "METADATA", "ownerId": "user-1", "displayName": "Mine"}

    def test_the_runtime_resolves_its_reference_paths(self, aws):
        """`read_skill_file` resolves `references/<file>` against the manifest."""
        from agents.main_agent.skills.strands_mapping import _resolve_resource

        seed.seed_skill_creator(TABLE, REGION)
        skill = SkillDefinition.from_dynamo_item(_row(aws))
        for path in ("references/writing-guide.md", "references/testing.md", "references/NOTICE.md"):
            assert _resolve_resource(skill, path) is not None, path


def test_main_seeds_it():
    source = Path(seed.__file__).read_text(encoding="utf-8")
    assert "seed_skill_creator(table_name=app_roles_table, region=region)" in source
