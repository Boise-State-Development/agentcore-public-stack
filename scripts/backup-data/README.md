# Pre-Migration Backup Tool

One-shot tooling that takes a complete, restore-friendly backup of all
application data and users in an AgentCore Public Stack deployment, so the
infrastructure can be torn down and redeployed in a new shape without losing
anything.

**Restore is intentionally a separate step.** This tool produces portable,
transformable artifacts (DynamoDB JSON, raw S3 objects, JSON config blobs);
the restore script — written against the *new* infrastructure once it
exists — reads those artifacts and maps them into the new shape.

## What is backed up

| Component | Source | How |
|---|---|---|
| Application DynamoDB tables (~20) | `/{prefix}/.../...-table-name` SSM params + the conventionally-named `{prefix}-assistants` table | `ExportTableToPointInTime` → gzipped DynamoDB-JSON in S3 |
| User-content S3 buckets | `/{prefix}/.../bucket-name` SSM params (user file uploads, RAG documents, conditional artifacts/fine-tuning) | `aws s3 sync` |
| Cognito User Pool config | `/{prefix}/auth/cognito/user-pool-id` SSM | `DescribeUserPool` JSON |
| **Cognito Identity Providers** | enumerate + describe each | full `ProviderDetails` incl. **OIDC `client_secret`** for automated re-registration |
| **Cognito App Clients** | enumerate + describe each | full record incl. **`ClientSecret`** |
| Cognito Resource Servers, Domain, UI Customization | describe APIs | JSON |
| Cognito Users, Groups, Group Memberships | paginated list APIs | gzipped JSONL |
| AgentCore Memory | `/{prefix}/inference-api/memory-id` SSM | memory config + best-effort per-actor events |

Every run lands in a freshly-created S3 bucket named
`{project_prefix}-backup-{utc_timestamp}` (versioned, SSE-encrypted, TLS-only,
all public access blocked) with this layout:

```
s3://{project_prefix}-backup-{ts}/{project_prefix}/{ts}/
  manifest.json                            # see manifest_schema.json
  dynamodb/
    {logical}.schema.json                  # KeySchema, GSIs, TTL, etc.
    {logical}/AWSDynamoDB/{export-id}/...  # DynamoDB-managed export tree
  s3/
    {logical}/                             # mirror of source bucket
  cognito/
    user-pool.json
    identity-providers.json                # ⚠ contains client_secrets
    app-clients.json                       # ⚠ contains ClientSecrets
    resource-servers.json
    domain.json
    ui-customization.json
    users.jsonl.gz
    groups.jsonl.gz
    group-memberships.jsonl.gz
  agentcore-memory/
    memory.json
    events.jsonl.gz                        # best-effort
    NOTES.md                               # only if partial / unavailable
```

## What is NOT backed up

- **Cognito password hashes** — Cognito does not expose them. Native-password
  users will need a "forgot password" reset on first login after restore.
  Federated (OIDC/SAML) users are unaffected.
- Ephemeral tables (`bff-sessions`, `oidc-state`, `voice-ticket-replay`) —
  TTL-driven, no value preserving across migration. Pass
  `--include-ephemeral` if you disagree.
- Vector index bucket (`/{prefix}/rag/vector-bucket-name`) — derived from
  the RAG documents bucket; re-ingest after restore.
- Frontend, MCP-sandbox, and other build-artifact buckets — rebuilt from
  source.
- KMS keys, Secrets Manager values, ECR images, CloudWatch logs — infra,
  not application data.

## ⚠ Sensitive data warning

The backup bucket contains:
- Every user's email, custom attributes, and (federated) identity links.
- Every user's uploaded files and RAG documents.
- **OIDC client secrets** for every federated IdP.
- **App client secrets** for every Cognito app client.
- **OAuth user tokens** (in the `oauth-user-tokens` DynamoDB table).

Treat the backup bucket like a production secret store. The bucket policy
denies non-TLS access and blocks all public access by default, but you are
responsible for restricting who can read it. After restore is complete and
verified, delete the bucket (and its versions).

## Running it

### From GitHub Actions (recommended)

`Actions → Backup Data (Pre-Migration) → Run workflow`. Inputs:

- `project_prefix` — e.g. `boisestate-prod`
- `aws_region` — defaults to `us-west-2`
- `aws_environment` — selects the GitHub Environment (and thus
  `AWS_ROLE_ARN` secret + any required approvals)
- `include_ephemeral` / `dry_run` / `allow_partial` — usually leave off

The workflow uses the existing `.github/actions/configure-aws-credentials`
composite action for OIDC role assumption.

### Locally

```bash
cd scripts/backup-data
uv sync
uv run python backup.py \
  --project-prefix boisestate-prod \
  --region us-west-2 \
  --dry-run            # start here to see what would happen
```

Requires AWS credentials with the IAM policy below. The script also shells
out to `aws s3 sync`, so the AWS CLI must be on PATH.

## IAM policy

The OIDC role (or local IAM user) needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DiscoverViaSSM",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:GetParametersByPath"],
      "Resource": "arn:aws:ssm:*:*:parameter/PROJECT_PREFIX/*"
    },
    {
      "Sid": "DynamoDBExport",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:ExportTableToPointInTime",
        "dynamodb:DescribeExport",
        "dynamodb:ListExports"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:*:table/PROJECT_PREFIX-*",
        "arn:aws:dynamodb:*:*:table/PROJECT_PREFIX-*/export/*"
      ]
    },
    {
      "Sid": "S3Source",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetObject", "s3:GetObjectTagging"],
      "Resource": [
        "arn:aws:s3:::PROJECT_PREFIX-*",
        "arn:aws:s3:::PROJECT_PREFIX-*/*"
      ]
    },
    {
      "Sid": "S3BackupBucket",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:PutBucketVersioning",
        "s3:PutBucketEncryption",
        "s3:PutBucketTagging",
        "s3:PutBucketPolicy",
        "s3:PutBucketPublicAccessBlock",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:GetObject"
      ],
      "Resource": [
        "arn:aws:s3:::PROJECT_PREFIX-backup-*",
        "arn:aws:s3:::PROJECT_PREFIX-backup-*/*"
      ]
    },
    {
      "Sid": "Cognito",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:DescribeUserPool",
        "cognito-idp:ListIdentityProviders",
        "cognito-idp:DescribeIdentityProvider",
        "cognito-idp:ListUserPoolClients",
        "cognito-idp:DescribeUserPoolClient",
        "cognito-idp:ListResourceServers",
        "cognito-idp:DescribeUserPoolDomain",
        "cognito-idp:GetUICustomization",
        "cognito-idp:ListUsers",
        "cognito-idp:ListGroups",
        "cognito-idp:ListUsersInGroup"
      ],
      "Resource": "arn:aws:cognito-idp:*:*:userpool/*"
    },
    {
      "Sid": "AgentCoreMemory",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetMemory",
        "bedrock-agentcore:ListActors",
        "bedrock-agentcore:ListSessions",
        "bedrock-agentcore:ListEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "STSCallerIdentity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "KMSForEncryptedResources",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": "*"
    }
  ]
}
```

Replace `PROJECT_PREFIX` with your actual project prefix (e.g.
`boisestate-prod`).

## Verification built into the tool

- DynamoDB: each export records `ItemCount`, `BilledSizeBytes`, and the
  `ExportManifest` location.
- S3: source object count + total bytes are compared to destination count
  after sync; mismatch fails that component.
- Cognito: per-component counts; OIDC IdPs missing a `client_secret`
  fail loudly (rather than silently writing an unrestorable backup).
- `manifest.json` rolls everything up into `summary.{ok,skipped,failed}`.
  The workflow fails on any `failed` row unless `--allow-partial` is set.

## Restore (placeholder)

Out of scope here. The restore script will:

1. Read `manifest.json` to know what exists.
2. For each `dynamodb/{logical}/` export, read DynamoDB-JSON line by line
   and `BatchWriteItem` into the new table with whatever attribute
   re-mapping the new schema requires.
3. For each `s3/{logical}/`, `aws s3 sync` back into the new bucket (or
   re-key on the way through).
4. For Cognito: re-create the user pool config (via CDK in the new infra),
   then iterate `identity-providers.json` + `app-clients.json` to
   `CreateIdentityProvider` / `CreateUserPoolClient` using the preserved
   secrets — which is the whole reason we capture them.
5. For users: `AdminCreateUser` with `MessageAction=SUPPRESS` then optionally
   trigger a forced password reset.
6. For AgentCore Memory: re-create the memory resource in the new infra,
   then replay `events.jsonl.gz` if needed.
