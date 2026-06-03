---
name: cdk-infrastructure
description: AWS CDK infrastructure development with TypeScript. Use when creating or modifying CDK constructs, DynamoDB tables, ECS/Fargate services, Lambda functions, S3 buckets, networking, IAM roles, or any CloudFormation resources. Covers configuration patterns, single-stack architecture, naming conventions, and Bedrock AgentCore integration.
---

# AWS CDK Infrastructure Best Practices

## TypeScript

- Use strict type checking
- Import from `aws-cdk-lib` and `constructs`
- Use L2 constructs when available, L1 (Cfn*) when necessary

## Architecture — Single Stack

The entire application is provisioned by **one CDK stack** (`PlatformStack`). Application code is shipped out-of-band via AWS APIs (ECR push → ECS service update / Lambda code update / AgentCore Runtime update).

```
infrastructure/
├── bin/infrastructure.ts          # App entrypoint (instantiates PlatformStack)
├── lib/
│   ├── platform-stack.ts          # The one stack — all infrastructure
│   ├── config.ts                  # Configuration loader & validator
│   └── constructs/                # 39 reusable CDK constructs
│       ├── network/               # VPC, ALB, ECS cluster
│       ├── identity/              # Cognito, secrets, KMS, OAuth
│       ├── data/                  # DynamoDB tables, file uploads
│       ├── rag/                   # RAG documents, vectors
│       ├── rag-ingestion/         # RAG ingestion Lambda
│       ├── artifacts/             # Artifact rendering pipeline
│       ├── mcp-sandbox/           # MCP Apps sandbox proxy
│       ├── agentcore/             # Memory, Code Interpreter, Browser, Gateway
│       ├── inference-api/         # AgentCore Runtime
│       ├── app-api/               # Fargate service
│       ├── fine-tuning/           # SageMaker IAM
│       ├── spa/                   # SPA CloudFront distribution
│       └── zones/                 # Route53, ALB DNS
└── cdk.context.json               # Configuration defaults
```

**Key principle:** CDK deploys are rare (infrastructure changes only). Day-to-day code changes deploy via `backend.yml` (AWS API calls, no CDK).

## Configuration

Use the centralized config system:

```typescript
import { loadConfig, getResourceName, getStackEnv, applyStandardTags } from './config';
```

PlatformStack receives config via props:
```typescript
const config = loadConfig(app);
new PlatformStack(app, `${config.projectPrefix}-PlatformStack`, { config, env });
```

For configuration patterns, see [references/configuration.md](references/configuration.md).

## Naming Conventions

**Resource Names:** Use `getResourceName()`:
```typescript
getResourceName(config, 'user-quotas')  // "bsu-agentcore-user-quotas"
```

**SSM Parameters:** Hierarchical naming for runtime consumption:
```
/{projectPrefix}/{category}/{resource-type}
```

Categories: `/network/`, `/quota/`, `/cost-tracking/`, `/auth/`, `/frontend/`, `/gateway/`, `/rag/`, `/artifacts/`

## Cross-Construct References

Since everything is in one stack, use **typed props** — not SSM:

```typescript
// In PlatformStack:
const network = new NetworkConstruct(this, 'Network', { config });
new AlbConstruct(this, 'Alb', { config, vpc: network.vpc });
```

SSM parameters are published **only for runtime consumption** by ECS tasks and Lambdas — never for CDK-to-CDK references within the same stack.

## DynamoDB Tables

- Always use PK + SK for flexibility
- Use `PAY_PER_REQUEST` billing
- Enable point-in-time recovery
- Environment-based removal policy

For table patterns, see [references/dynamodb.md](references/dynamodb.md).

## ECS/Fargate

- Cluster created by NetworkConstruct, referenced via typed prop
- Health checks mandatory
- Auto-scaling with CPU/memory targets
- Circuit breaker for rollback
- Bootstrap container pattern: CDK creates the service with a placeholder image; the backend workflow pushes the real image via `update-service`

For service patterns, see [references/ecs-fargate.md](references/ecs-fargate.md).

## Lambda

- Use ARM64 architecture (cost optimization)
- Role with least privilege
- Secrets Manager access requires wildcard suffix
- Bootstrap pattern: CDK creates the function with placeholder code; the backend workflow pushes real code via `update-function-code`

For Lambda patterns, see [references/lambda.md](references/lambda.md).

## S3 Buckets

- Block public access
- Enable versioning
- Lifecycle rules for cost optimization
- Include account ID for global uniqueness

For bucket patterns, see [references/s3.md](references/s3.md).

## Security

- Separate security groups for ALB and ECS
- Private subnets for services
- IAM roles with SIDs for clarity
- Never hardcode secrets

For IAM patterns, see [references/iam.md](references/iam.md).

## Important Constraints

**AgentCore Names:** Use underscores, not hyphens:
```typescript
name: getResourceName(config, 'memory').replace(/-/g, '_')
```

**Secrets Manager ARN:** Include wildcard for random suffix:
```typescript
resources: [`${secret.secretArn}*`]
```

**Removal Policy:**
```typescript
removalPolicy: getRemovalPolicy(config)  // RETAIN in prod, DESTROY in dev
```

## CDK Commands

```bash
cd infrastructure
npm ci                # Install dependencies
npx cdk synth         # Synthesize CloudFormation
npx cdk deploy {prefix}-PlatformStack  # Deploy
npx cdk diff          # Preview changes
```
