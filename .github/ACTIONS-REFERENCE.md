# GitHub Actions Configuration Reference

## Introduction

This document provides a comprehensive reference for all GitHub Variables and Secrets used in the AgentCore Public Stack deployment workflows. All AWS resources are provisioned by a single CDK stack — `PlatformStack`, deployed by `platform.yml` — while application code ships out-of-band via `backend.yml` and the SPA via `frontend-deploy.yml`. The **Subsystem** column in the table below names the functional area within `PlatformStack` that each value tunes; it is **not** a separate deployable stack, and there is no deploy ordering between subsystems.

For a quick-start guide with only the required values, see [README-ACTIONS.md](./README-ACTIONS.md).

## GitHub Variables vs Secrets

GitHub provides two mechanisms for storing configuration values:

- **Variables**: Non-sensitive configuration values stored in repository settings and accessed via `vars.*` in workflows. Use Variables for values like AWS regions, project prefixes, and resource sizing parameters that don't need encryption.

- **Secrets**: Sensitive configuration values stored encrypted in repository settings and accessed via `secrets.*` in workflows. Use Secrets for values like AWS credentials, API keys, certificate ARNs, and any other sensitive data that should never be exposed in logs or workflow files.

## Complete Configuration Reference

> **Single stack:** every CDK resource below is deployed by one stack — `PlatformStack` (`infrastructure/lib/platform-stack.ts`). The **Subsystem** column indicates which functional area each variable tunes; it is not a separate stack to deploy or an ordering between features.

| Name | Type | Required | Default | Subsystem | Description |
|------|------|----------|---------|-----------|-------------|
| AWS_ACCESS_KEY_ID | Secret | No | None | All | AWS access key ID for authentication (alternative to role-based auth) |
| AWS_REGION | Variable | Yes | `us-west-2` | All | AWS region for resource deployment |
| AWS_ROLE_ARN | Secret | No | None | All | AWS IAM role ARN for OIDC authentication (recommended over access keys) |
| AWS_SECRET_ACCESS_KEY | Secret | No | None | All | AWS secret access key for authentication (alternative to role-based auth) |
| CDK_ALB_SUBDOMAIN | Variable | No | None | Platform | Subdomain for ALB (e.g., 'api' for api.yourdomain.com) |
| CDK_APP_API_CPU | Variable | No | `512` | App API | CPU units for App API ECS task (256, 512, 1024, 2048, 4096) |
| CDK_APP_API_CORS_ORIGINS | Variable | No | None | App API | Additional CORS origins for the app API only (appended to global CORS origins) |
| CDK_APP_API_DESIRED_COUNT | Variable | No | `1` | App API | Desired number of App API tasks running |
| CDK_APP_API_ENABLED | Variable | No | `true` | App API | Enable/disable App API deployment |
| CDK_APP_API_MAX_CAPACITY | Variable | No | `10` | App API | Maximum App API tasks for auto-scaling |
| CDK_APP_API_MEMORY | Variable | No | `1024` | App API | Memory (MB) for App API ECS task (512, 1024, 2048, 4096, 8192) |
| CDK_ARTIFACTS_CERTIFICATE_ARN | Variable | No | Falls back to `CDK_CLOUDFRONT_CERTIFICATE_ARN` | Artifacts | **Optional override** for the artifacts origin (`artifacts.{CDK_DOMAIN_NAME}`) cert. Leave unset to use the shared `CDK_CLOUDFRONT_CERTIFICATE_ARN`; set only to give this origin a different cert. **Must be in `us-east-1`.** Same one-label wildcard-depth rule as the shared cert. An effective cert (this or the shared var) is required for a domained deploy — synth fails otherwise. |
| CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS | Variable | No | None | Artifacts | Comma-separated extra origins (beyond `https://{CDK_DOMAIN_NAME}`) permitted to embed artifact iframes via CSP `frame-ancestors` — e.g. `http://localhost:4200` for a local SPA pointed at this deployment. Applied to both the CloudFront response-headers policy and the render Lambda's CSP. **Leave unset in production**: every listed origin can frame users' artifacts (still render-token gated, but a real loosening on a shared environment). |
| CDK_ARTIFACTS_RETENTION_DAYS | Variable | No | `90` | Artifacts | Days after which soft-deleted artifacts (objects tagged `lifecycle-class=deleted`) are reaped by the S3 lifecycle rule. |
| CDK_ASSISTANTS_CORS_ORIGINS | Variable | No | None | Platform | Additional CORS origins for the assistants module only (appended to global CORS origins) |
| CDK_AWS_ACCOUNT | Variable | Yes | None | All | 12-digit AWS account ID for CDK deployment |
| CDK_CERTIFICATE_ARN | Variable | No | None | Platform | ACM certificate ARN for HTTPS on the ALB. Must be in the **deployment region** (not us-east-1). |
| CDK_CLOUDFRONT_CERTIFICATE_ARN | Variable | No | None | Platform | Shared ACM certificate ARN for all CloudFront origins (SPA, artifacts, mcp-sandbox). **Must be in `us-east-1`** and cover `{CDK_DOMAIN_NAME}` + `*.{CDK_DOMAIN_NAME}`. Each CloudFront origin falls back to this when its section-specific cert var is unset, so one wildcard satisfies all three. **Effectively required for any deploy with `CDK_DOMAIN_NAME` set** (unless every per-origin cert var is supplied individually) — a domained deploy with no effective CloudFront cert fails at `cdk synth`. |
| CDK_CORS_ORIGINS | Variable | No | None | All | Additional CORS origins appended to the auto-derived `https://{CDK_DOMAIN_NAME}`. Comma-separated. Use for localhost during local dev (e.g., `http://localhost:4200`) or extra domains. |
| CDK_DOMAIN_NAME | Variable | No | None | All | Primary domain name (e.g., 'alpha.boisestate.ai'). Auto-applied as `https://{value}` to CORS origins platform-wide. This is the primary mechanism for CORS configuration. |
| CDK_FILE_UPLOAD_CORS_ORIGINS | Variable | No | None | Platform | Additional CORS origins for the file upload S3 bucket only (appended to global CORS origins) |
| CDK_FILE_UPLOAD_MAX_SIZE_MB | Variable | No | `10` | Platform | Maximum file upload size in megabytes |
| CDK_FINE_TUNING_CORS_ORIGINS | Variable | No | None | SageMaker Fine-Tuning | Additional CORS origins for the fine-tuning S3 bucket only (appended to global CORS origins) |
| CDK_FINE_TUNING_DEFAULT_QUOTA_HOURS | Variable | No | `0` | App API | Default monthly GPU-hour quota for all authenticated users. `0` = whitelist-only (admin must grant each user). Positive value (e.g. `5`) = open access with that default budget. |
| CDK_FRONTEND_BUCKET_NAME | Variable | No | None | Frontend | S3 bucket name for frontend assets (defaults to generated name with account ID) |
| CDK_FRONTEND_CORS_ORIGINS | Variable | No | None | Frontend | Additional CORS origins for the frontend SSM export only (appended to global CORS origins) |
| CDK_FRONTEND_CERTIFICATE_ARN | Variable | No | Falls back to `CDK_CLOUDFRONT_CERTIFICATE_ARN` | Frontend | **Optional override** for the SPA origin (`{CDK_DOMAIN_NAME}`) cert. Leave unset to use the shared `CDK_CLOUDFRONT_CERTIFICATE_ARN`; set only to give the SPA a different cert. **Must be in `us-east-1`.** |
| CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS | Variable | No | `PriceClass_100` | Frontend | CloudFront price class (PriceClass_100, PriceClass_200, PriceClass_All) |
| CDK_FRONTEND_ENABLED | Variable | No | `true` | Frontend | Enable/disable Frontend deployment |
| CDK_GATEWAY_API_TYPE | Variable | No | `HTTP` | Gateway | API Gateway type for Gateway (REST or HTTP) |
| CDK_GATEWAY_ENABLE_WAF | Variable | No | `false` | Gateway | Enable AWS WAF for Gateway API protection |
| CDK_GATEWAY_ENABLED | Variable | No | `true` | Gateway | Enable/disable Gateway deployment |
| CDK_GATEWAY_LOG_LEVEL | Variable | No | `INFO` | Gateway | Log level for Lambda functions (DEBUG, INFO, WARNING, ERROR) |
| CDK_GATEWAY_THROTTLE_BURST_LIMIT | Variable | No | `5000` | Gateway | API Gateway burst limit for throttling (requests) |
| CDK_GATEWAY_THROTTLE_RATE_LIMIT | Variable | No | `10000` | Gateway | API Gateway rate limit for throttling (requests per second) |
| CDK_HOSTED_ZONE_DOMAIN | Variable | No | None | Platform | Route53 hosted zone domain name (e.g., 'example.com') |
| CDK_INFERENCE_API_CPU | Variable | No | `1024` | Inference API | CPU units for Inference API AgentCore Runtime (256, 512, 1024, 2048, 4096) |
| CDK_INFERENCE_API_CORS_ORIGINS | Variable | No | None | Inference API | Additional CORS origins for the inference API only (appended to global CORS origins) |
| CDK_INFERENCE_API_DESIRED_COUNT | Variable | No | `1` | Inference API | Desired number of Inference API runtime instances |
| CDK_INFERENCE_API_ENABLED | Variable | No | `true` | Inference API | Enable/disable Inference API deployment |
| CDK_INFERENCE_API_MAX_CAPACITY | Variable | No | `5` | Inference API | Maximum Inference API runtime instances for auto-scaling |
| CDK_INFERENCE_API_MEMORY | Variable | No | `2048` | Inference API | Memory (MB) for Inference API AgentCore Runtime (512, 1024, 2048, 4096, 8192) |
| CDK_MCP_SANDBOX_CERTIFICATE_ARN | Variable | No | Falls back to `CDK_CLOUDFRONT_CERTIFICATE_ARN` | MCP Sandbox | **Optional override** for the MCP Apps sandbox origin (`mcp-sandbox.{CDK_DOMAIN_NAME}`) cert — the cross-origin shell the SPA frames MCP Apps in. Leave unset to use the shared `CDK_CLOUDFRONT_CERTIFICATE_ARN`; set only to give this origin a different cert. **Must be in `us-east-1`.** An effective cert (this or the shared var) is required for a domained deploy — without it the proxy would land on the CloudFront default domain with no Route 53 ALIAS and MCP Apps fail to load, so synth fails instead. |
| CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS | Variable | No | None | MCP Sandbox | Comma-separated extra origins (beyond `https://{CDK_DOMAIN_NAME}`) permitted to embed the MCP Apps sandbox proxy via CSP `frame-ancestors` — e.g. `http://localhost:4200` for a local SPA pointed at this deployment. **Leave unset in production.** |
| CDK_PRODUCTION | Variable | No | `true` | Frontend | Production environment flag (affects runtime config generation) |
| CDK_PROJECT_PREFIX | Variable | Yes | `agentcore` | All | Prefix for all resource names (e.g., 'mycompany-agentcore') |
| CDK_RAG_CORS_ORIGINS | Variable | No | None | RAG Ingestion | Additional CORS origins for the RAG documents S3 bucket only (appended to global CORS origins) |
| CDK_RETAIN_DATA_ON_DELETE | Variable | No | `false` | All | Retain data resources (DynamoDB, S3, Secrets) on stack deletion |
| CDK_VPC_CIDR | Variable | No | `10.0.0.0/16` | Platform | CIDR block for VPC network |
| ENV_INFERENCE_API_CORS_ORIGINS | Variable | No | None | Inference API | _(Deprecated — use CDK_INFERENCE_API_CORS_ORIGINS instead)_ |
| ENV_INFERENCE_API_LOG_LEVEL | Variable | No | `INFO` | Inference API | Log level for runtime container (DEBUG, INFO, WARNING, ERROR) |
| SEED_ADMIN_JWT_ROLE | Variable | No | None | Bootstrap Data Seeding | _(Deprecated)_ Previously used for JWT role mapping. Admin access is now granted automatically via the Cognito first-boot flow. |
