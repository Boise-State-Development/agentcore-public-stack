# GitHub Actions Configuration Reference

## Introduction

This document provides a comprehensive reference for all GitHub Variables and Secrets used in the AgentCore Public Stack deployment workflows. The table below shows all configuration values across the six deployment workflows: Infrastructure, App API, Inference API, Frontend, Gateway, and Bootstrap Data Seeding.

For a quick-start guide with only the required values, see [README-ACTIONS.md](./README-ACTIONS.md).

## GitHub Variables vs Secrets

GitHub provides two mechanisms for storing configuration values:

- **Variables**: Non-sensitive configuration values stored in repository settings and accessed via `vars.*` in workflows. Use Variables for values like AWS regions, project prefixes, and resource sizing parameters that don't need encryption.

- **Secrets**: Sensitive configuration values stored encrypted in repository settings and accessed via `secrets.*` in workflows. Use Secrets for values like AWS credentials, API keys, certificate ARNs, and any other sensitive data that should never be exposed in logs or workflow files.

## Complete Configuration Reference

| Name | Type | Required | Default | Used In | Description |
|------|------|----------|---------|---------|-------------|
| AWS_ACCESS_KEY_ID | Secret | No | None | All | AWS access key ID for authentication (alternative to role-based auth) |
| AWS_REGION | Variable | Yes | `us-west-2` | All | AWS region for resource deployment |
| AWS_ROLE_ARN | Secret | No | None | All | AWS IAM role ARN for OIDC authentication (recommended over access keys) |
| AWS_SECRET_ACCESS_KEY | Secret | No | None | All | AWS secret access key for authentication (alternative to role-based auth) |
| CDK_ALB_SUBDOMAIN | Variable | No | None | Infrastructure | Subdomain for ALB (e.g., 'api' for api.yourdomain.com) |
| CDK_APP_API_CPU | Variable | No | `512` | Infrastructure, App API | CPU units for App API ECS task (256, 512, 1024, 2048, 4096) |
| CDK_APP_API_DESIRED_COUNT | Variable | No | `1` | Infrastructure, App API | Desired number of App API tasks running |
| CDK_APP_API_ENABLED | Variable | No | `true` | App API | Enable/disable App API stack deployment |
| CDK_APP_API_MAX_CAPACITY | Variable | No | `10` | Infrastructure, App API | Maximum App API tasks for auto-scaling |
| CDK_APP_API_MEMORY | Variable | No | `1024` | Infrastructure, App API | Memory (MB) for App API ECS task (512, 1024, 2048, 4096, 8192) |
| CDK_AWS_ACCOUNT | Variable | Yes | None | All | 12-digit AWS account ID for CDK deployment |
| CDK_CERTIFICATE_ARN | Variable | No | None | Infrastructure | ACM certificate ARN for HTTPS on ALB |
| CDK_CORS_ORIGINS | Variable | No | `http://localhost:4200,http://localhost:8000` | All | Top-level CORS origins (default for sections that don't override) |
| CDK_DOMAIN_NAME | Variable | No | None | Frontend, App API | Custom domain name (e.g., 'app.example.com') |
| CDK_FILE_UPLOAD_CORS_ORIGINS | Variable | No | `http://localhost:4200` | Infrastructure, App API | Comma-separated CORS origins for file upload S3 bucket |
| CDK_FILE_UPLOAD_MAX_SIZE_MB | Variable | No | `10` | Infrastructure, App API | Maximum file upload size in megabytes |
| CDK_FINE_TUNING_ENABLED | Variable | No | `false` | SageMaker Fine-Tuning, App API | Enable SageMaker fine-tuning stack and App API fine-tuning routes. Must be `true` before deploying the SageMaker Fine-Tuning workflow. |
| CDK_FINE_TUNING_DEFAULT_QUOTA_HOURS | Variable | No | `0` | App API | Default monthly GPU-hour quota for all authenticated users. `0` = whitelist-only (admin must grant each user). Positive value (e.g. `5`) = open access with that default budget. |
| CDK_FRONTEND_BUCKET_NAME | Variable | No | None | Frontend | S3 bucket name for frontend assets (defaults to generated name with account ID) |
| CDK_FRONTEND_CERTIFICATE_ARN | Variable | No | None | Frontend | ACM certificate ARN for HTTPS on CloudFront (required for custom domain) |
| CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS | Variable | No | `PriceClass_100` | Frontend | CloudFront price class (PriceClass_100, PriceClass_200, PriceClass_All) |
| CDK_FRONTEND_ENABLED | Variable | No | `true` | Frontend | Enable/disable Frontend stack deployment |
| CDK_GATEWAY_API_TYPE | Variable | No | `HTTP` | Gateway | API Gateway type for Gateway (REST or HTTP) |
| CDK_GATEWAY_ENABLE_WAF | Variable | No | `false` | Gateway | Enable AWS WAF for Gateway API protection |
| CDK_GATEWAY_ENABLED | Variable | No | `true` | Gateway | Enable/disable Gateway stack deployment |
| CDK_GATEWAY_LOG_LEVEL | Variable | No | `INFO` | Gateway | Log level for Lambda functions (DEBUG, INFO, WARNING, ERROR) |
| CDK_GATEWAY_THROTTLE_BURST_LIMIT | Variable | No | `5000` | Gateway | API Gateway burst limit for throttling (requests) |
| CDK_GATEWAY_THROTTLE_RATE_LIMIT | Variable | No | `10000` | Gateway | API Gateway rate limit for throttling (requests per second) |
| CDK_HOSTED_ZONE_DOMAIN | Variable | No | None | Infrastructure, App API | Route53 hosted zone domain name (e.g., 'example.com') |
| CDK_INFERENCE_API_CPU | Variable | No | `1024` | Infrastructure, Inference API | CPU units for Inference API AgentCore Runtime (256, 512, 1024, 2048, 4096) |
| CDK_INFERENCE_API_DESIRED_COUNT | Variable | No | `1` | Infrastructure, Inference API | Desired number of Inference API runtime instances |
| CDK_INFERENCE_API_ENABLED | Variable | No | `true` | Inference API | Enable/disable Inference API stack deployment |
| CDK_INFERENCE_API_MAX_CAPACITY | Variable | No | `5` | Infrastructure, Inference API | Maximum Inference API runtime instances for auto-scaling |
| CDK_INFERENCE_API_MEMORY | Variable | No | `2048` | Infrastructure, Inference API | Memory (MB) for Inference API AgentCore Runtime (512, 1024, 2048, 4096, 8192) |
| CDK_PRODUCTION | Variable | No | `true` | Frontend | Production environment flag (affects runtime config generation) |
| CDK_PROJECT_PREFIX | Variable | Yes | `agentcore` | All | Prefix for all resource names (e.g., 'mycompany-agentcore') |
| CDK_RETAIN_DATA_ON_DELETE | Variable | No | `false` | All | Retain data resources (DynamoDB, S3, Secrets) on stack deletion |
| CDK_VPC_CIDR | Variable | No | `10.0.0.0/16` | Infrastructure, App API | CIDR block for VPC network |
| ENV_INFERENCE_API_CORS_ORIGINS | Variable | No | None | Inference API | Comma-separated CORS origins for runtime environment |
| ENV_INFERENCE_API_LOG_LEVEL | Variable | No | `INFO` | Inference API | Log level for runtime container (DEBUG, INFO, WARNING, ERROR) |
| SEED_ADMIN_JWT_ROLE | Variable | No | None | Bootstrap Data Seeding | JWT role that grants system admin access (e.g., `Admin`). Maps to the `system_admin` AppRole. |
| SEED_AUTH_BUTTON_COLOR | Variable | No | None | Bootstrap Data Seeding | Hex color for the auth provider login button (e.g., '#0078D4') |
| SEED_AUTH_CLIENT_ID | Variable | No | None | Bootstrap Data Seeding | OAuth client ID for the initial OIDC auth provider |
| SEED_AUTH_CLIENT_SECRET | Secret | No | None | Bootstrap Data Seeding | OAuth client secret for the initial OIDC auth provider |
| SEED_AUTH_DISPLAY_NAME | Variable | No | None | Bootstrap Data Seeding | Display name shown on the login page (e.g., 'Microsoft Entra ID') |
| SEED_AUTH_ISSUER_URL | Variable | No | None | Bootstrap Data Seeding | OIDC issuer URL for the auth provider (e.g., 'https://login.microsoftonline.com/TENANT/v2.0') |
| SEED_AUTH_PROVIDER_ID | Variable | No | None | Bootstrap Data Seeding | Slug identifier for the auth provider (e.g., 'entra-id') |
