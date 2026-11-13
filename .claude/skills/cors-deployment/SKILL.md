---
name: cors-deployment
description: CORS configuration across the CDK PlatformStack, GitHub Actions workflows, and Python backends. Use when modifying CORS origins, adding a construct that needs CORS, debugging CORS errors in deployed environments, or touching any workflow env vars related to CDK_DOMAIN_NAME or CDK_CORS_ORIGINS.
---

# CORS Deployment Configuration

## Architecture

CORS is configured via a layered model applied identically to every construct in `PlatformStack`:

1. `CDK_DOMAIN_NAME` → auto-applied as `https://{value}` (always)
2. `CDK_CORS_ORIGINS` → additional global origins (optional, comma-separated)
3. Per-section `CDK_*_CORS_ORIGINS` → stack-specific extras (optional)

localhost is NEVER auto-included. Use `CDK_CORS_ORIGINS=http://localhost:4200` for local dev.

## The Helper

Every construct uses `buildCorsOrigins(config, additionalOrigins?)` from `infrastructure/lib/config.ts`. This returns a deduplicated `string[]`.

```typescript
// Container env var (Fargate / AgentCore Runtime)
CORS_ORIGINS: buildCorsOrigins(config, config.appApi.additionalCorsOrigins).join(','),

// S3 bucket CORS rule
cors: [{ allowedOrigins: buildCorsOrigins(config, config.fileUpload?.additionalCorsOrigins) }]
```

## Config Derivation (config.ts)

```
CDK_DOMAIN_NAME → domainName → "https://{domainName}"  (always first)
CDK_CORS_ORIGINS → extraCorsOrigins                     (appended)
Result: config.corsOrigins = "https://{domainName},{extras}"
```

Both are joined into `config.corsOrigins`. The helper then splits, deduplicates, and optionally appends section extras.

## Python Backend

Both `app_api/main.py` and `inference_api/main.py` read `CORS_ORIGINS` env var:

```python
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",")
```

No hardcoded fallback. If `CORS_ORIGINS` is empty, no origins are allowed.

## Workflow Requirements

`CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` MUST be in the **job-level** `env:` block (not workflow-level) because they use `vars.*` which requires `environment:` on the job.

Every workflow that runs synth or deploy must include:
```yaml
env:
  CDK_DOMAIN_NAME: ${{ vars.CDK_DOMAIN_NAME }}
  CDK_CORS_ORIGINS: ${{ vars.CDK_CORS_ORIGINS }}
```

## Per-Section Config Interfaces

Every config section that consumes CORS has `additionalCorsOrigins?: string`:
- `FrontendConfig.additionalCorsOrigins`
- `AppApiConfig.additionalCorsOrigins`
- `InferenceApiConfig.additionalCorsOrigins`
- `RagIngestionConfig.additionalCorsOrigins`
- `FineTuningConfig.additionalCorsOrigins`

The file-upload bucket has no section extra: it uses `buildCorsOrigins(config)`.

## Adding CORS to a New Construct

1. Import `buildCorsOrigins` from `infrastructure/lib/config.ts` (relative to the construct's folder under `lib/constructs/<area>/`)
2. Call `buildCorsOrigins(config, config.mySection.additionalCorsOrigins)`
3. Add `additionalCorsOrigins?: string` to the section's config interface
4. Load it in `loadConfig()`: `additionalCorsOrigins: process.env.CDK_MY_SECTION_CORS_ORIGINS || ...`
5. Add `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` to the workflow job env
6. Add a test beside the existing `buildCorsOrigins` cases in `infrastructure/test/config.test.ts`

## Common Mistakes

- Putting `vars.*` in workflow-level `env:` → resolves to empty string
- Hardcoding `http://localhost:4200` in buildCorsOrigins or Python fallback
- Forgetting to add `CDK_DOMAIN_NAME` to a new workflow's synth/deploy jobs
- Using `config.domainName` directly instead of `buildCorsOrigins()`
- Setting `corsOrigins` in `cdk.context.json` (overrides domain derivation)
