---
title: Platform (CDK)
description: Deploy the single PlatformStack.
sidebar:
  order: 2
---

`platform.yml` runs `cdk deploy` and stands up every AWS resource in one stack —
`PlatformStack`. It's the first workflow you run on a fresh account, and the one
you return to only when infrastructure actually changes. Before the first run,
you need a few things set up in AWS and wired into your GitHub fork.

## What `platform.yml` does

The workflow runs `scripts/platform/deploy.sh`, which:

1. **`cdk synth`** — produces the CloudFormation template under `cdk.out/`.
2. **`cdk-assets publish`** — pushes the *bootstrap* container images (tiny
   HTTP servers that answer ALB and AgentCore health checks) to ECR.
3. **Seeds the image-tag parameters** — writes `/<prefix>/<service>/image-tag`
   to SSM with the bootstrap URI, but **only if the parameter doesn't already
   exist**. On later runs this step is a no-op; the build pipeline owns the
   parameter from then on.
4. **`cdk deploy {prefix}-PlatformStack`** — provisions everything.

After a first deploy, the ECS service and AgentCore Runtime are running the
bootstrap stub. `backend.yml` then rolls them over to the real service images.

:::note
The compute image URIs are read from SSM at CloudFormation deploy time, so an
infra-only re-deploy always picks up the latest *live* image — it never reverts
a running service back to the bootstrap stub.
:::

## AWS prerequisites

Set these up once in the AWS Console before configuring GitHub.

### Authentication

GitHub Actions needs credentials to deploy. Choose **one** method.

- **OIDC role (recommended).** Create a GitHub OIDC identity provider in IAM and
  an IAM role that trusts it, then attach deploy permissions. No long-lived keys
  to rotate. Note the **role ARN**.
- **IAM access keys (simpler, less secure).** Create an IAM user with
  programmatic access and generate an access key pair. Note the **access key ID**
  and **secret access key**.

The simplest permission setup is an account with `AdministratorAccess`. A
least-privilege role must cover IAM, VPC, ECS, ECR, ALB, Route 53, ACM,
CloudFront, S3, DynamoDB, Lambda, API Gateway, CloudWatch, and Secrets Manager.

### Route 53 hosted zone

Create a **public hosted zone** for your domain (e.g. `example.com`). If the
domain is registered outside AWS, update the registrar's nameservers to point at
the zone's NS records. This is done once per domain — skip it if a zone already
exists.

### ACM certificates

You need **two** TLS certificates, each covering both your apex and wildcard
subdomains (`example.com` and `*.example.com`):

| Certificate | Region | Used by |
|-------------|--------|---------|
| **ALB certificate** | Your deployment region (e.g. `us-west-2`) | Application Load Balancer (`api.example.com`) |
| **CloudFront certificate** | `us-east-1` (required) | Frontend CDN (`app.example.com`) |

Use **DNS validation** — if the domain is in Route 53, ACM can create the
validation records for you. Don't proceed until both certificates show
**Issued**.

:::caution[Mind the wildcard depth]
The stack **always** provisions two CloudFront subdomain origins that need TLS
coverage in `us-east-1`: `artifacts.{CDK_DOMAIN_NAME}` (artifact iframes) and
`mcp-sandbox.{CDK_DOMAIN_NAME}` (the cross-origin shell that frames MCP Apps). A
TLS wildcard covers **exactly one** label — `*.example.com` matches
`artifacts.example.com` but **not** `artifacts.alpha.example.com`.

- If `CDK_DOMAIN_NAME` is your **apex** (`example.com`), the existing
  `*.example.com` CloudFront cert covers both origins — reuse that ARN.
- If `CDK_DOMAIN_NAME` is **already a subdomain** (`alpha.example.com`), issue a
  dedicated `us-east-1` cert for `*.alpha.example.com` — a single wildcard covers
  both `artifacts.` and `mcp-sandbox.` — and use that ARN for both certificate
  variables below.

A domained deploy whose sandbox cert is missing **fails at `cdk synth`** rather
than shipping an unreachable origin. Verify a cert's coverage before deploying:

```bash
aws acm describe-certificate --region us-east-1 --certificate-arn <arn> \
  --query 'Certificate.SubjectAlternativeNames'
```
:::

### X-Ray Transaction Search

Transaction Search is an **account-level singleton** — it can't be managed by
CloudFormation if it already exists, so enable it once via the CLI (replace
`PARTITION`, `REGION`, and `ACCOUNT_ID`):

```bash
# 1. CloudWatch Logs resource policy for X-Ray
aws logs put-resource-policy \
  --policy-name XRayTransactionSearchPolicy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "TransactionSearchXRayAccess",
      "Effect": "Allow",
      "Principal": { "Service": "xray.amazonaws.com" },
      "Action": "logs:PutLogEvents",
      "Resource": [
        "arn:PARTITION:logs:REGION:ACCOUNT_ID:log-group:aws/spans:*",
        "arn:PARTITION:logs:REGION:ACCOUNT_ID:log-group:/aws/application-signals/data:*"
      ],
      "Condition": {
        "ArnLike": { "aws:SourceArn": "arn:PARTITION:xray:REGION:ACCOUNT_ID:*" },
        "StringEquals": { "aws:SourceAccount": "ACCOUNT_ID" }
      }
    }]
  }'

# 2. Send trace segments to CloudWatch Logs
aws xray update-trace-segment-destination --destination CloudWatchLogs

# 3. Set the indexing sampling percentage (5% is a reasonable start)
aws xray update-indexing-rule --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 5}}'
```

Skip this if Transaction Search is already enabled in the account.

## GitHub configuration

In your fork, go to **Settings → Secrets and variables → Actions**. The
workflows read these at runtime.

### Secrets

Add your AWS credentials as repository **secrets** — never commit them.

| Secret | When | Value |
|--------|------|-------|
| `AWS_ROLE_ARN` | Using OIDC | IAM role ARN |
| `AWS_ACCESS_KEY_ID` | Using access keys | Access key ID |
| `AWS_SECRET_ACCESS_KEY` | Using access keys | Secret access key |

### Variables

Switch to the **Variables** tab. These eight are the minimum for a first deploy.

| Variable | Example | Description |
|----------|---------|-------------|
| `AWS_REGION` | `us-west-2` | AWS region for all resources |
| `CDK_AWS_ACCOUNT` | `123456789012` | Your 12-digit AWS account ID |
| `CDK_PROJECT_PREFIX` | `agentcore` | Unique prefix for all resource names |
| `CDK_HOSTED_ZONE_DOMAIN` | `example.com` | Route 53 hosted zone domain |
| `CDK_ALB_SUBDOMAIN` | `api` | Subdomain for the API load balancer |
| `CDK_DOMAIN_NAME` | `app.example.com` | Full domain for the frontend |
| `CDK_CERTIFICATE_ARN` | `arn:aws:acm:us-west-2:…` | ALB certificate ARN |
| `CDK_FRONTEND_CERTIFICATE_ARN` | `arn:aws:acm:us-east-1:…` | CloudFront certificate ARN |

### Feature certificates

Artifacts, the MCP Apps sandbox, and SageMaker fine-tuning are **always
provisioned** — there are no `CDK_*_ENABLED` flags. When `CDK_DOMAIN_NAME` is
set, the artifacts and MCP-sandbox CloudFront origins each need a `us-east-1`
certificate ARN. **A domained deploy that omits either fails at `cdk synth`** —
it aborts before shipping an origin with no Route 53 record rather than silently
degrading to the CloudFront default domain.

| Variable | Description |
|----------|-------------|
| `CDK_ARTIFACTS_CERTIFICATE_ARN` | Covers `artifacts.{CDK_DOMAIN_NAME}`, in `us-east-1`. See the wildcard-depth note above. |
| `CDK_MCP_SANDBOX_CERTIFICATE_ARN` | Covers `mcp-sandbox.{CDK_DOMAIN_NAME}`, in `us-east-1`. A single `*.{CDK_DOMAIN_NAME}` cert covers both, so you can reuse the same ARN. |

For optional settings — ECS sizing, CloudFront price class, CORS origins,
retention, and the rest — see
[Environments](/agentcore-public-stack/deployment/environments/) and the
[full configuration reference](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/.github/ACTIONS-REFERENCE.md).

## What gets provisioned

A single `cdk deploy` creates all of it:

- **Networking** — VPC, public and private subnets across two AZs, an ALB with
  HTTPS listeners, and Route 53 aliases.
- **Identity** — Cognito User Pool and app client (first-boot admin signup), KMS
  keys for OAuth token encryption and BFF cookie signing, and Secrets Manager
  for OAuth client secrets.
- **Data** — ~24 DynamoDB tables and 6 S3 buckets, all encrypted at rest with
  public access fully blocked.
- **AgentCore** — Memory, Code Interpreter, Browser, Gateway, and the Runtime
  resource (initially pointed at the inference-api bootstrap image).
- **Compute** — the App API ECS Fargate service and task definition, plus the
  RAG ingestion and artifact render Lambdas (initially on bootstrap images).
- **Edge** — CloudFront distributions for the SPA, the artifacts subdomain, and
  the MCP sandbox subdomain, with OAC-only S3 reads and strict CSP response
  headers.
- **ML** — the SageMaker execution IAM role and security group for fine-tuning.

Resources you don't use sit idle at zero or near-zero cost.

## Re-running `platform.yml`

Re-run it only when you change `infrastructure/lib/**` — new tables, new IAM
grants, network changes, and the like. Routine code and SPA changes ship through
`backend.yml` and `frontend-deploy.yml` and never need a platform re-deploy. The
first run takes ~15–20 minutes; subsequent runs only ship the delta.
