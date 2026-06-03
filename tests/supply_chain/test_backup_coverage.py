"""
Backup/Restore coverage verification.

Scans the CDK constructs for stateful resources (DynamoDB tables, S3 buckets,
Cognito user pools, AgentCore Memory) and verifies that the backup tool's
resource inventory covers every one of them.

This is the "reflection" test — it ensures the backup/restore framework
stays in sync with the infrastructure as new resources are added.

Run with: pytest tests/supply_chain/test_backup_coverage.py -v
"""

import ast
import os
import re
from pathlib import Path

import pytest

# Paths
REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRUCTS_DIR = REPO_ROOT / "infrastructure" / "lib" / "constructs"
PLATFORM_STACK = REPO_ROOT / "infrastructure" / "lib" / "platform-stack.ts"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup-data" / "backup.py"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore-data" / "restore.py"


def extract_table_names_from_constructs() -> set[str]:
    """Scan all construct .ts files for DynamoDB table names."""
    tables = set()
    pattern = re.compile(r"getResourceName\(config,\s*'([^']+)'\)")

    for ts_file in CONSTRUCTS_DIR.rglob("*.ts"):
        if ".d.ts" in ts_file.name:
            continue
        content = ts_file.read_text()
        # Only look at lines near "new dynamodb.Table" or "tableName:"
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "tableName:" in line or "new dynamodb.Table" in line:
                # Check this line and next few for getResourceName
                context = "\n".join(lines[max(0, i - 1) : i + 5])
                for match in pattern.finditer(context):
                    tables.add(match.group(1))

    return tables


def extract_bucket_names_from_constructs() -> set[str]:
    """Scan all construct .ts files for S3 bucket name prefixes."""
    buckets = set()
    pattern = re.compile(r"getResourceName\(config,\s*'([^']+)'")

    for ts_file in CONSTRUCTS_DIR.rglob("*.ts"):
        if ".d.ts" in ts_file.name:
            continue
        content = ts_file.read_text()
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "bucketName:" in line or "new s3.Bucket" in line:
                context = "\n".join(lines[max(0, i - 1) : i + 5])
                for match in pattern.finditer(context):
                    name = match.group(1)
                    # Filter out non-bucket names (e.g. 'frontend-oac')
                    if any(
                        x in name
                        for x in ["oac", "cache", "headers", "strip", "routing", "sg", "tg", "task"]
                    ):
                        continue
                    buckets.add(name)

    return buckets


def extract_backup_tables() -> set[str]:
    """Parse the backup script to find which tables it backs up."""
    content = BACKUP_SCRIPT.read_text()
    tables = set()

    # Match "logical": "xxx" entries in DYNAMODB_TABLES and DYNAMODB_TABLES_BY_CONVENTION
    for match in re.finditer(r'"logical":\s*"([^"]+)"', content):
        tables.add(match.group(1))

    return tables


def extract_backup_buckets() -> set[str]:
    """Parse the backup script to find which S3 buckets it backs up."""
    content = BACKUP_SCRIPT.read_text()
    buckets = set()

    # Find the S3_BUCKETS block (between "S3_BUCKETS" and the next blank line or variable)
    in_s3_section = False
    for line in content.split("\n"):
        if "S3_BUCKETS" in line and "list[" in line:
            in_s3_section = True
            continue
        if in_s3_section:
            if line.strip() == "]" or (line.strip() and not line.startswith(" ") and not line.startswith("{")):
                break
            match = re.search(r'"logical":\s*"([^"]+)"', line)
            if match:
                buckets.add(match.group(1))

    return buckets


def extract_restore_tables() -> set[str]:
    """Parse the restore script to find which tables it can restore."""
    content = RESTORE_SCRIPT.read_text()
    tables = set()

    # TABLE_SSM_MAP keys
    for match in re.finditer(r'"([^"]+)":\s*"/', content):
        tables.add(match.group(1))

    # TABLE_CONVENTION_MAP keys
    convention_section = re.search(r"TABLE_CONVENTION_MAP.*?\}", content, re.DOTALL)
    if convention_section:
        for match in re.finditer(r'"([^"]+)":\s*"', convention_section.group()):
            if "/" not in match.group(1):  # skip SSM paths
                tables.add(match.group(1))

    return tables


# ============================================================
# Tests
# ============================================================


class TestBackupCoversAllTables:
    """Every DynamoDB table in the CDK constructs must be in the backup inventory."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.cdk_tables = extract_table_names_from_constructs()
        self.backup_tables = extract_backup_tables()
        self.restore_tables = extract_restore_tables()

    def test_cdk_tables_found(self):
        """Sanity: we found tables in the CDK code."""
        assert len(self.cdk_tables) >= 20, f"Expected ≥20 tables, found {len(self.cdk_tables)}"

    def test_backup_tables_found(self):
        """Sanity: we found tables in the backup script."""
        assert len(self.backup_tables) >= 20, f"Expected ≥20 tables, found {len(self.backup_tables)}"

    def test_every_cdk_table_is_backed_up(self):
        """Every table defined in CDK constructs must appear in the backup inventory."""
        # CDK resource names → backup logical names (when they differ)
        cdk_to_backup_aliases = {
            "user-artifacts": "artifacts",  # SSM resolves to same table
        }
        mapped_cdk = set()
        for t in self.cdk_tables:
            mapped_cdk.add(cdk_to_backup_aliases.get(t, t))

        missing = mapped_cdk - self.backup_tables
        assert missing == set(), (
            f"Tables in CDK but NOT in backup script:\n"
            + "\n".join(f"  - {t}" for t in sorted(missing))
            + "\n\nAdd them to DYNAMODB_TABLES or DYNAMODB_TABLES_BY_CONVENTION "
            + "in scripts/backup-data/backup.py"
        )

    def test_every_cdk_table_is_restorable(self):
        """Every table defined in CDK constructs must appear in the restore inventory."""
        # CDK resource names → restore logical names (when they differ)
        cdk_to_restore_aliases = {
            "user-artifacts": "artifacts",
        }
        # Ephemeral tables don't need restore
        ephemeral = {"bff-sessions", "oidc-state", "voice-ticket-replay"}
        mapped_cdk = set()
        for t in self.cdk_tables:
            if t in ephemeral:
                continue
            mapped_cdk.add(cdk_to_restore_aliases.get(t, t))

        missing = mapped_cdk - self.restore_tables
        assert missing == set(), (
            f"Tables in CDK but NOT in restore script:\n"
            + "\n".join(f"  - {t}" for t in sorted(missing))
            + "\n\nAdd them to TABLE_SSM_MAP or TABLE_CONVENTION_MAP "
            + "in scripts/restore-data/restore.py"
        )

    def test_no_phantom_backup_tables(self):
        """Backup script shouldn't reference tables that don't exist in CDK."""
        # Ephemeral tables (bff-sessions, oidc-state, voice-ticket-replay)
        # are intentionally in backup but may not be in the "required" CDK set.
        # Some backup logical names differ from CDK resource names:
        #   backup "artifacts" → CDK "user-artifacts"
        ephemeral = {"bff-sessions", "oidc-state", "voice-ticket-replay"}
        # Logical names in backup that map to different CDK names
        known_aliases = {"artifacts", "fine-tuning-data", "rag-documents"}
        phantom = self.backup_tables - self.cdk_tables - ephemeral - known_aliases
        assert phantom == set(), (
            f"Tables in backup script but NOT in CDK constructs:\n"
            + "\n".join(f"  - {t}" for t in sorted(phantom))
        )


class TestBackupCoversAllBuckets:
    """Every user-data S3 bucket in the CDK constructs must be in the backup inventory."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.cdk_buckets = extract_bucket_names_from_constructs()
        self.backup_buckets = extract_backup_buckets()

    def test_cdk_buckets_found(self):
        """Sanity: we found buckets in the CDK code."""
        assert len(self.cdk_buckets) >= 3, f"Expected ≥3 buckets, found {len(self.cdk_buckets)}"

    def test_user_data_buckets_are_backed_up(self):
        """Every user-data bucket must be in the backup inventory.

        Excludes:
          - mcp-sandbox (static shell, no user data)
          - frontend (build artifacts, reproducible from source)

        CDK resource name → backup logical name mapping:
          - artifacts-content → artifacts (SSM resolves)
          - rag-documents → rag-documents (matches)
          - user-file-uploads → user-file-uploads (matches)
          - fine-tuning-data → fine-tuning-data (matches)
        """
        non_data_buckets = {"mcp-sandbox", "frontend"}
        cdk_to_backup = {
            "artifacts-content": "artifacts",
        }
        data_buckets = self.cdk_buckets - non_data_buckets
        mapped = {cdk_to_backup.get(b, b) for b in data_buckets}
        missing = mapped - self.backup_buckets
        assert missing == set(), (
            f"Data buckets in CDK but NOT in backup script:\n"
            + "\n".join(f"  - {t}" for t in sorted(missing))
            + "\n\nAdd them to S3_BUCKETS in scripts/backup-data/backup.py"
        )


class TestBackupCoversCognito:
    """The backup script must back up Cognito."""

    def test_cognito_backup_exists(self):
        content = BACKUP_SCRIPT.read_text()
        assert "cognito" in content.lower()
        assert "DescribeUserPool" in content or "describe_user_pool" in content
        assert "ListUsers" in content or "list_users" in content
        assert "ListGroups" in content or "list_groups" in content

    def test_cognito_idp_secrets_preserved(self):
        """Identity provider client secrets must be captured for re-registration."""
        content = BACKUP_SCRIPT.read_text()
        assert "client_secret" in content.lower() or "ClientSecret" in content


class TestBackupCoversAgentCoreMemory:
    """The backup script must attempt AgentCore Memory backup."""

    def test_memory_backup_exists(self):
        content = BACKUP_SCRIPT.read_text()
        assert "agentcore-memory" in content or "memory" in content.lower()
        assert "memory-id" in content or "MEMORY_ID" in content


class TestRestoreCoversAllBackedUpTables:
    """The restore script must handle every table the backup produces."""

    def test_restore_covers_backup(self):
        backup_tables = extract_backup_tables()
        restore_tables = extract_restore_tables()
        # Ephemeral tables may be backed up but not restored (by design)
        ephemeral = {"bff-sessions", "oidc-state", "voice-ticket-replay"}
        required_restore = backup_tables - ephemeral
        missing = required_restore - restore_tables
        assert missing == set(), (
            f"Tables backed up but NOT restorable:\n"
            + "\n".join(f"  - {t}" for t in sorted(missing))
            + "\n\nAdd them to TABLE_SSM_MAP in scripts/restore-data/restore.py"
        )


class TestRestoreIsIdempotent:
    """The restore script must handle re-runs gracefully."""

    def test_catches_duplicate_provider(self):
        content = RESTORE_SCRIPT.read_text()
        assert "DuplicateProviderException" in content

    def test_catches_username_exists(self):
        content = RESTORE_SCRIPT.read_text()
        assert "UsernameExistsException" in content

    def test_catches_group_exists(self):
        content = RESTORE_SCRIPT.read_text()
        assert "GroupExistsException" in content

    def test_has_dry_run(self):
        content = RESTORE_SCRIPT.read_text()
        assert "--dry-run" in content
