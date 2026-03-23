# Configuration Variable Inventory

Complete inventory of all configuration variables across the AgentCore Public Stack, organized by layer.

## 1. Backend Environment Variables (`backend/src/.env.example`)

| Variable | Required | Default | Consuming Module(s) |
|----------|----------|---------|---------------------|
| AWS_REGION | Yes | `us-west-2` | All AWS SDK calls |
| AWS_PROFILE | No | `default` | AWS credential chain |
| AGENTCORE_MEMORY_TYPE | No | `file` | `agents/main_agent/session/` |
| AGENTCORE_MEMORY_ID | Conditional | — | `agents/main_agent/session/` (required when MEMORY_TYPE=dynamodb) |
| AGENTCORE_MEMORY_RELEVANCE_SCORE | No | `0.7` | `agents/main_agent/session/session_factory.py` |
| AGENTCORE_MEMORY_TOP_K | No | `10` | `agents/main_agent/session/session_factory.py` |
| AGENTCORE_MEMORY_COMPACTION_ENABLED | No | `false` | `agents/main_agent/session/` |
| AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD | No | `100000` | `agents/main_agent/session/` |
| AGENTCORE_MEMORY_COMPACTION_PROTECTED_TURNS | No | `2` | `agents/main_agent/session/` |
| AGENTCORE_MEMORY_COMPACTION_MAX_TOOL_CONTENT_LENGTH | No | `500` | `agents/main_agent/session/` |
| AGENTCORE_GATEWAY_MCP_ENABLED | No | `true` | `agents/main_agent/integrations/external_mcp_client.py` |
| AGENTCORE_CODE_INTERPRETER_ID | No | — | `agents/builtin_tools/code_interpreter_diagram_tool.py` |
| ENABLE_QUOTA_ENFORCEMENT | No | `true` | `agents/main_agent/quota/` |
| UPLOAD_DIR | No | `uploads` | `apis/inference_api/` (local-dev-only) |
| OUTPUT_DIR | No | `output` | `apis/inference_api/` (local-dev-only) |
| GENERATED_IMAGES_DIR | No | `generated_images` | `apis/inference_api/` (local-dev-only) |
| DYNAMODB_MANAGED_MODELS_TABLE_NAME | No | — | `apis/app_api/` model management |
| DYNAMODB_SESSIONS_METADATA_TABLE_NAME | No | — | `apis/app_api/messages/`, cost tracking |
| DYNAMODB_COST_SUMMARY_TABLE_NAME | No | — | `apis/app_api/costs/` |
| DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME | No | — | `apis/app_api/costs/` admin dashboard |
| DYNAMODB_OIDC_STATE_TABLE_NAME | No | — | `apis/shared/auth/` |
| DYNAMODB_QUOTA_TABLE | No | — | `agents/main_agent/quota/` |
| DYNAMODB_QUOTA_EVENTS_TABLE | No | — | `agents/main_agent/quota/` |
| DYNAMODB_USERS_TABLE_NAME | No | — | `apis/app_api/users/` |
| DYNAMODB_APP_ROLES_TABLE_NAME | No | — | `apis/shared/rbac/` |
| DYNAMODB_USER_FILES_TABLE_NAME | No | — | `apis/app_api/files/` |
| DYNAMODB_AUTH_PROVIDERS_TABLE_NAME | No | — | `apis/shared/auth/` |
| DYNAMODB_ASSISTANTS_TABLE_NAME | No | — | `apis/app_api/assistants/` |
| DYNAMODB_USER_SETTINGS_TABLE_NAME | No | — | `apis/shared/user_settings/` |
| DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME | No | — | `apis/app_api/` OAuth management |
| DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME | No | — | `apis/app_api/` OAuth tokens |
| AUTH_PROVIDER_SECRETS_ARN | No | — | `apis/shared/auth/` |
| OAUTH_TOKEN_ENCRYPTION_KEY_ARN | No | — | `apis/app_api/` OAuth encryption |
| OAUTH_CLIENT_SECRETS_ARN | No | — | `apis/app_api/` OAuth secrets |
| ADMIN_JWT_ROLES | No | `["DotNetDevelopers"]` | `apis/shared/rbac/` |
| FRONTEND_URL | No | `http://localhost:4200` | CORS configuration |
| CORS_ORIGINS | No | localhost list | `apis/app_api/main.py`, `apis/inference_api/main.py` |
| S3_USER_FILES_BUCKET_NAME | No | — | `apis/app_api/files/` |
| FILE_UPLOAD_MAX_SIZE_BYTES | No | `4194304` | `apis/app_api/files/` |
| FILE_UPLOAD_MAX_FILES_PER_MESSAGE | No | `5` | `apis/app_api/files/` |
| FILE_UPLOAD_USER_QUOTA_BYTES | No | `1073741824` | `apis/app_api/files/` |
| S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME | No | — | `apis/app_api/assistants/` |
| S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME | No | — | `apis/app_api/assistants/` |
| S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME | No | — | `apis/app_api/assistants/` |
| APP_ROLE_USER_CACHE_TTL_MINUTES | No | `5` | `apis/shared/rbac/` |
| APP_ROLE_ROLE_CACHE_TTL_MINUTES | No | `10` | `apis/shared/rbac/` |
| APP_ROLE_MAPPING_CACHE_TTL_MINUTES | No | `10` | `apis/shared/rbac/` |
| OPENAI_API_KEY | No | — | `agents/main_agent/core/model_config.py` |
| GOOGLE_GEMINI_API_KEY | No | — | `agents/main_agent/core/model_config.py` |

## 2. CDK Context Keys (`infrastructure/cdk.context.json` → `config.ts`)

| Context Key | Env Var Override | Type | Default | Config Field |
|-------------|-----------------|------|---------|-------------|
| `production` | `CDK_PRODUCTION` | boolean | `true` | `config.production` |
| `retainDataOnDelete` | `CDK_RETAIN_DATA_ON_DELETE` | boolean | `false` | `config.retainDataOnDelete` |
| `projectPrefix` | `CDK_PROJECT_PREFIX` | string | `agentcore` | `config.projectPrefix` |
| `awsAccount` | `CDK_AWS_ACCOUNT` | string | — (required) | `config.awsAccount` |
| `awsRegion` | `CDK_AWS_REGION` | string | `us-west-2` | `config.awsRegion` |
| `vpcCidr` | — | string | `10.0.0.0/16` | `config.vpcCidr` |
| `corsOrigins` | `CDK_CORS_ORIGINS` | string | `http://localhost:4200,http://localhost:8000` | `config.corsOrigins` |
| `domainName` | `CDK_DOMAIN_NAME` | string | `""` | `config.domainName` |
| `infrastructureHostedZoneDomain` | `CDK_HOSTED_ZONE_DOMAIN` | string | `""` | `config.infrastructureHostedZoneDomain` |
| `albSubdomain` | `CDK_ALB_SUBDOMAIN` | string | `""` | `config.albSubdomain` |
| `certificateArn` | `CDK_CERTIFICATE_ARN` | string | `""` | `config.certificateArn` |
| `imageTag` | — | string | `""` | `config.appApi.imageTag`, `config.inferenceApi.imageTag` |
| `frontend.certificateArn` | `CDK_FRONTEND_CERTIFICATE_ARN` | string | `""` | `config.frontend.certificateArn` |
| `frontend.enabled` | `CDK_FRONTEND_ENABLED` | boolean | `true` | `config.frontend.enabled` |
| `frontend.bucketName` | `CDK_FRONTEND_BUCKET_NAME` | string | `""` | `config.frontend.bucketName` |
| `frontend.cloudFrontPriceClass` | `CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS` | string | `PriceClass_100` | `config.frontend.cloudFrontPriceClass` |
| `appApi.enabled` | `CDK_APP_API_ENABLED` | boolean | `true` | `config.appApi.enabled` |
| `appApi.cpu` | `CDK_APP_API_CPU` | number | `512` | `config.appApi.cpu` |
| `appApi.memory` | `CDK_APP_API_MEMORY` | number | `1024` | `config.appApi.memory` |
| `appApi.desiredCount` | `CDK_APP_API_DESIRED_COUNT` | number | `1` | `config.appApi.desiredCount` |
| `appApi.maxCapacity` | `CDK_APP_API_MAX_CAPACITY` | number | `10` | `config.appApi.maxCapacity` |
| `inferenceApi.enabled` | `CDK_INFERENCE_API_ENABLED` | boolean | `true` | `config.inferenceApi.enabled` |
| `inferenceApi.cpu` | `CDK_INFERENCE_API_CPU` | number | `1024` | `config.inferenceApi.cpu` |
| `inferenceApi.memory` | `CDK_INFERENCE_API_MEMORY` | number | `2048` | `config.inferenceApi.memory` |
| `inferenceApi.desiredCount` | `CDK_INFERENCE_API_DESIRED_COUNT` | number | `1` | `config.inferenceApi.desiredCount` |
| `inferenceApi.maxCapacity` | `CDK_INFERENCE_API_MAX_CAPACITY` | number | `5` | `config.inferenceApi.maxCapacity` |
| `inferenceApi.logLevel` | `ENV_INFERENCE_API_LOG_LEVEL` | string | `INFO` | `config.inferenceApi.logLevel` |
| `inferenceApi.corsOrigins` | `ENV_INFERENCE_API_CORS_ORIGINS` | string | `""` | `config.inferenceApi.corsOrigins` |
| `gateway.enabled` | `CDK_GATEWAY_ENABLED` | boolean | `true` | `config.gateway.enabled` |
| `gateway.apiType` | `CDK_GATEWAY_API_TYPE` | `REST`\|`HTTP` | `HTTP` | `config.gateway.apiType` |
| `gateway.throttleRateLimit` | `CDK_GATEWAY_THROTTLE_RATE_LIMIT` | number | `10000` | `config.gateway.throttleRateLimit` |
| `gateway.throttleBurstLimit` | `CDK_GATEWAY_THROTTLE_BURST_LIMIT` | number | `5000` | `config.gateway.throttleBurstLimit` |
| `gateway.enableWaf` | `CDK_GATEWAY_ENABLE_WAF` | boolean | `false` | `config.gateway.enableWaf` |
| `gateway.logLevel` | `CDK_GATEWAY_LOG_LEVEL` | string | `INFO` | `config.gateway.logLevel` |
| `fileUpload.enabled` | `CDK_FILE_UPLOAD_ENABLED` | boolean | `true` | `config.fileUpload.enabled` |
| `fileUpload.maxFileSizeBytes` | `CDK_FILE_UPLOAD_MAX_FILE_SIZE` | number | `4194304` | `config.fileUpload.maxFileSizeBytes` |
| `fileUpload.maxFilesPerMessage` | `CDK_FILE_UPLOAD_MAX_FILES_PER_MESSAGE` | number | `5` | `config.fileUpload.maxFilesPerMessage` |
| `fileUpload.userQuotaBytes` | `CDK_FILE_UPLOAD_USER_QUOTA` | number | `1073741824` | `config.fileUpload.userQuotaBytes` |
| `fileUpload.retentionDays` | `CDK_FILE_UPLOAD_RETENTION_DAYS` | number | `365` | `config.fileUpload.retentionDays` |
| `fileUpload.corsOrigins` | `CDK_FILE_UPLOAD_CORS_ORIGINS` | string | (falls back to `corsOrigins`) | `config.fileUpload.corsOrigins` |
| `assistants.enabled` | `CDK_ASSISTANTS_ENABLED` | boolean | `true` | `config.assistants.enabled` |
| `assistants.corsOrigins` | `CDK_ASSISTANTS_CORS_ORIGINS` | string | (falls back to `corsOrigins`) | `config.assistants.corsOrigins` |
| `ragIngestion.enabled` | `CDK_RAG_ENABLED` | boolean | `true` | `config.ragIngestion.enabled` |
| `ragIngestion.corsOrigins` | `CDK_RAG_CORS_ORIGINS` | string | (falls back to `corsOrigins`) | `config.ragIngestion.corsOrigins` |
| `ragIngestion.lambdaMemorySize` | `CDK_RAG_LAMBDA_MEMORY` | number | `10240` | `config.ragIngestion.lambdaMemorySize` |
| `ragIngestion.lambdaTimeout` | `CDK_RAG_LAMBDA_TIMEOUT` | number | `900` | `config.ragIngestion.lambdaTimeout` |
| `ragIngestion.embeddingModel` | `CDK_RAG_EMBEDDING_MODEL` | string | `amazon.titan-embed-text-v2` | `config.ragIngestion.embeddingModel` |
| `ragIngestion.vectorDimension` | `CDK_RAG_VECTOR_DIMENSION` | number | `1024` | `config.ragIngestion.vectorDimension` |
| `ragIngestion.vectorDistanceMetric` | `CDK_RAG_DISTANCE_METRIC` | string | `cosine` | `config.ragIngestion.vectorDistanceMetric` |
| `tags` | — | object | `{ ManagedBy: "CDK" }` | `config.tags` (+ `Project` injected dynamically) |

## 3. Frontend Environment (`frontend/ai.client/src/environments/`)

| Field | File | Default | Consuming Service |
|-------|------|---------|-------------------|
| `production` | `environment.ts` | `false` | `ConfigService` (fallback only) |
| `appApiUrl` | `environment.ts` | `http://localhost:8000` | `ConfigService` (fallback only) |
| `production` | `environment.production.ts` | `true` | `ConfigService` (fallback only) |
| `appApiUrl` | `environment.production.ts` | `""` | `ConfigService` (fallback only) |

In production, the frontend loads runtime configuration from `/config.json` (generated by CDK FrontendStack). The `environment.ts` values are fallbacks only.

### Runtime Config (`/config.json`)

| Field | Source | Consuming Service |
|-------|--------|-------------------|
| `appApiUrl` | SSM `/{projectPrefix}/app-api/url` | `ConfigService.appApiUrl()` |
| `environment` | CDK `production` flag | `ConfigService.environment()` |

## 4. Configuration Precedence

```
Environment Variable  →  CDK Context (cdk.context.json)  →  Not Set (validation error or undefined)
```

- Required fields (`projectPrefix`, `awsAccount`, `awsRegion`) throw errors if missing from both sources.
- Optional fields return `undefined` if not set, and stacks handle the absence gracefully.
- CORS origins cascade: section-level → top-level `corsOrigins` → empty string.
- Tags: `ManagedBy` from context, `Project` injected dynamically from `projectPrefix`.

## 5. Removed Variables (Config Cleanup Audit)

The following variables were removed as dead configuration:

| Variable | Reason |
|----------|--------|
| `ENABLE_AUTHENTICATION` / `enableAuthentication` | Auth is always enabled; toggle removed |
| `inferenceApiUrl` | Frontend resolves inference endpoint dynamically at runtime |
| `enableRoute53` | Route53 derived from `domainName` presence |
| `enableGpu` | GPU support removed from inference API config |
| `enableRds` / `rdsInstanceClass` / `rdsEngine` / `rdsDatabaseName` | RDS never implemented |
| `databaseType` | Single database type (DynamoDB) |
| `uploadDir` / `outputDir` / `generatedImagesDir` (CDK) | Local-dev-only; removed from CDK config |
| `oauthCallbackUrl` (CDK InferenceApiConfig) | Derived from `domainName` in infrastructure stack |
| `apiUrl` / `frontendUrl` (CDK InferenceApiConfig) | Dead fields, never consumed by stacks |
| `entraClientId` / `entraTenantId` | Replaced by generic OIDC provider system |
