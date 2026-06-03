# Infrastructure

Single-stack CDK architecture for the AgentCore platform.

## Architecture

One CDK stack (`PlatformStack`) owns **all** AWS infrastructure. Application code is shipped out-of-band via AWS APIs (ECR push → ECS service update / Lambda code update / AgentCore Runtime update), not via CDK deploys.

```
PlatformStack
├── Network:     VPC, ALB, ECS cluster, security groups
├── Identity:    Cognito, Secrets Manager, KMS, WorkloadIdentity
├── Data:        ~24 DynamoDB tables, 6 S3 buckets
├── Edge:        CloudFront (SPA + artifacts + MCP sandbox)
├── AgentCore:   Memory, Code Interpreter, Browser, Gateway, Runtime
├── Compute:     App API Fargate service, RAG + artifact render Lambdas
├── ML:          SageMaker execution role + security group
└── DNS:         Route53 hosted zone + alias records
```

## Deploy

```bash
cd infrastructure

# Install dependencies
npm ci

# Synthesize
npx cdk synth

# Deploy infrastructure
npx cdk deploy {prefix}-PlatformStack

# Diff (check what would change)
npx cdk diff
```

Infrastructure deploys are rare — only when adding tables, changing IAM grants, etc. Day-to-day code changes go through `backend.yml` (AWS API calls, no CDK).

## Deploy Scripts

| Script | Purpose |
|--------|---------|
| `scripts/platform/deploy.sh` | CDK deploy of PlatformStack |
| `scripts/platform/synth.sh` | CDK synth only |
| `scripts/build/build-all-images.sh` | Content-hash Docker builds for all services |
| `scripts/frontend/build.sh` | Angular SPA production build |
| `scripts/frontend/deploy.sh` | S3 sync + CloudFront invalidation |
| `scripts/teardown/destroy.sh` | Destroy all stacks (cleanup) |

## Content-Hash Docker Builds

Container images are tagged with a SHA-256 hash of their source inputs (Dockerfile + tracked source files + dependency manifests). The build pipeline skips `docker build` + `docker push` when ECR already has an image with that tag.

```bash
# Build all images (skips unchanged)
scripts/build/build-all-images.sh

# Compute hash for a single service
scripts/build/compute-content-hash.sh \
  --dockerfile backend/Dockerfile.app-api \
  --source-dir backend/src \
  --manifest backend/pyproject.toml
```

## Constructs

The stack is composed of 39 reusable constructs under `lib/constructs/`:

```
constructs/
  network/        — VPC, ALB, ECS cluster
  identity/       — Cognito, auth secrets, KMS, OAuth, WorkloadIdentity
  data/           — DynamoDB tables, file uploads
  rag/            — RAG documents bucket, vectors, assistants table
  rag-ingestion/  — RAG ingestion Lambda
  artifacts/      — Artifacts DDB + S3 + render Lambda + CloudFront
  mcp-sandbox/    — MCP Apps sandbox proxy S3 + CloudFront
  agentcore/      — Memory, Code Interpreter, Browser, Gateway
  inference-api/  — AgentCore Runtime
  app-api/        — Fargate service + task definition
  fine-tuning/    — SageMaker IAM + data tables
  spa/            — SPA S3 bucket + CloudFront distribution
  zones/          — Route53, ALB DNS
```

## Prerequisites

- AWS CLI configured with appropriate credentials
- Node.js 22+
- Docker (for container image builds)
- `CDK_PROJECT_PREFIX`, `CDK_AWS_REGION`, `CDK_AWS_ACCOUNT` environment variables set

## Legacy Migration

If migrating from the previous multi-stack architecture, back up data first (`scripts/backup-data/`), then delete the old CloudFormation stacks:

```bash
aws cloudformation delete-stack --stack-name {prefix}-InfrastructureStack
aws cloudformation delete-stack --stack-name {prefix}-AppApiStack
aws cloudformation delete-stack --stack-name {prefix}-InferenceApiStack
aws cloudformation delete-stack --stack-name {prefix}-GatewayStack
aws cloudformation delete-stack --stack-name {prefix}-RagIngestionStack
aws cloudformation delete-stack --stack-name {prefix}-SageMakerFineTuningStack
aws cloudformation delete-stack --stack-name {prefix}-ArtifactsStack
aws cloudformation delete-stack --stack-name {prefix}-McpSandboxStack
aws cloudformation delete-stack --stack-name {prefix}-FrontendStack
```

Then deploy the new single stack and restore data (`scripts/restore-data/`).
