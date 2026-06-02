# Restore Tool

Reads a backup produced by `scripts/backup-data/` and imports all data into a
deployed AgentCore two-stack environment (PlatformStack + BackendStack).

## Prerequisites

1. The target environment must be **fully deployed** (PlatformStack + BackendStack).
   The restore tool resolves target table names and bucket names from SSM.
2. AWS credentials with sufficient permissions (see IAM policy below).
3. Python 3.13+ with `uv` installed.

## Usage

```bash
cd scripts/restore-data

# Install dependencies
uv sync

# Dry run (prints what would be restored without writing)
uv run python restore.py \
  --backup-bucket myprefix-backup-20260520T173042Z \
  --manifest-key myprefix/20260520T173042Z/manifest.json \
  --target-prefix myprefix \
  --region us-west-2 \
  --dry-run

# Full restore
uv run python restore.py \
  --backup-bucket myprefix-backup-20260520T173042Z \
  --manifest-key myprefix/20260520T173042Z/manifest.json \
  --target-prefix myprefix \
  --region us-west-2
```

## What gets restored

| Component | Source in backup | How |
|---|---|---|
| DynamoDB tables (~20) | `dynamodb/{logical}/` export tree | Read DynamoDB-JSON line by line, `BatchWriteItem` into target table |
| S3 buckets | `s3/{logical}/` mirror | `aws s3 sync` from backup bucket to target bucket |
| Cognito Identity Providers | `cognito/identity-providers.json` | `CreateIdentityProvider` with preserved client secrets |
| Cognito Users | `cognito/users.jsonl.gz` | `AdminCreateUser` with `MessageAction=SUPPRESS` |
| Cognito Groups + Memberships | `cognito/groups.jsonl.gz` + `group-memberships.jsonl.gz` | `CreateGroup` + `AdminAddUserToGroup` |

## What is NOT restored automatically

- **Cognito password hashes** — not exportable by AWS. Native-password users
  will need a "forgot password" reset on first login. Federated (OIDC/SAML)
  users are unaffected.
- **Cognito App Clients** — CDK manages these. The backup preserves client
  secrets for reference if manual re-registration is needed.
- **AgentCore Memory events** — best-effort in the backup; the restore tool
  logs a note but does not replay events (the memory resource is recreated
  fresh by BackendStack's AgentCore Runtime construct).
- **Ephemeral tables** (bff-sessions, oidc-state, voice-ticket-replay) —
  TTL-driven, no value preserving.

## Idempotency

The tool is safe to re-run:

- **DynamoDB**: `put_item` overwrites existing items with the same key.
- **S3**: `aws s3 sync` is naturally idempotent (only copies changed objects).
- **Cognito IdPs**: `DuplicateProviderException` is caught and skipped.
- **Cognito Users**: `UsernameExistsException` is caught and skipped.
- **Cognito Groups**: `GroupExistsException` is caught and skipped.

## Options

| Flag | Description |
|------|-------------|
| `--backup-bucket` | S3 bucket containing the backup (required) |
| `--manifest-key` | S3 key of `manifest.json` in the backup bucket (required) |
| `--target-prefix` | CDK project prefix of the target environment (required) |
| `--region` | AWS region of the target environment (required) |
| `--dry-run` | Print what would be restored without writing |
| `--skip-cognito-users` | Skip user import (useful if users will self-register) |
| `--profile` | AWS CLI profile name |

## IAM Policy (minimum)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadBackupBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::PROJECT_PREFIX-backup-*",
        "arn:aws:s3:::PROJECT_PREFIX-backup-*/*"
      ]
    },
    {
      "Sid": "WriteTargetBuckets",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::PROJECT_PREFIX-*",
        "arn:aws:s3:::PROJECT_PREFIX-*/*"
      ]
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:BatchWriteItem",
        "dynamodb:PutItem",
        "dynamodb:DescribeTable"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/PROJECT_PREFIX-*"
    },
    {
      "Sid": "SSMRead",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:*:*:parameter/PROJECT_PREFIX/*"
    },
    {
      "Sid": "Cognito",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:CreateIdentityProvider",
        "cognito-idp:AdminCreateUser",
        "cognito-idp:CreateGroup",
        "cognito-idp:AdminAddUserToGroup"
      ],
      "Resource": "arn:aws:cognito-idp:*:*:userpool/*"
    }
  ]
}
```

Replace `PROJECT_PREFIX` with your actual project prefix.

## Workflow integration

A GitHub Actions workflow (`restore-data.yml`) can be added to run this tool
via `workflow_dispatch`:

```yaml
name: Restore Data
on:
  workflow_dispatch:
    inputs:
      backup_bucket:
        description: 'Backup bucket name'
        required: true
      manifest_key:
        description: 'Manifest key in backup bucket'
        required: true
      target_prefix:
        description: 'Target CDK project prefix'
        required: true
```
