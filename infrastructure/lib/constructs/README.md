# Constructs

Reusable CDK constructs that compose into `PlatformStack` and
`BackendStack`. Organized by **logical resource group**, not by legacy
stack origin — a single legacy stack often splits into two (data →
Platform, compute → Backend), and a single Platform/Backend stack draws
from many legacy origins.

## Layout

```
constructs/
  # Platform constructs (consumed by PlatformStack)
  network/        — VPC, subnets, NAT, ALB, listener, security groups
  identity/       — Cognito, auth secret, BFF cookie key, WorkloadIdentity
  data/           — Shared DynamoDB tables, OAuth tables, file-upload bucket
  rag/            — RAG documents bucket, vectors bucket, RAG DDB tables
  artifacts/      — Artifacts DDB + S3 + CloudFront + render-token secret
  mcp-sandbox/    — MCP Apps sandbox-proxy S3 + CloudFront + Function
  fine-tuning/    — Fine-tuning DDB tables + S3 bucket
  spa/            — SPA S3 bucket + CloudFront distribution
  zones/          — Route53 hosted zone (when domain configured)

  # Backend constructs (consumed by BackendStack)
  ecr/            — ECR repositories (app-api, inference-api, mcp-shared)
  app-api/        — App-API Fargate service + task def + target group
  inference-api/  — AgentCore Runtime + Memory + Code Interpreter + Browser
  gateway/        — AgentCore Gateway + role + 5 MCP Lambdas
  rag-ingestion/  — RAG ingestion Lambda + IAM + S3 notification
```

## Conventions

- One construct class per file, named after the file (e.g.
  `network/alb-construct.ts` exports `class AlbConstruct`).
- Each construct accepts a typed `<Name>ConstructProps` interface that
  includes the resolved `AppConfig` plus any cross-stack typed inputs
  the construct needs (e.g. `AppApiServiceConstruct` accepts
  `vpc: ec2.IVpc`, `albListener: elbv2.IApplicationListener`, etc.).
- `public readonly` properties expose the resources the parent stack
  needs to forward elsewhere (typed against L2 interfaces — `IVpc`,
  `IBucket`, `ITable` — not concrete classes).
- SSM parameter publication that the application reads at runtime stays
  inside the construct that owns the resource. Cross-stack reads via
  SSM do NOT happen — the parent stack passes typed objects via
  construct props.
- IAM grants (`bucket.grantRead(role)`, `table.grantReadWriteData(role)`)
  flow through the typed objects rather than through SSM ARN strings,
  so refactoring an ARN format never silently breaks a permission.
