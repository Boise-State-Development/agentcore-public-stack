---
name: cors-deployment
description: The deployment's allowed-origins list (CORS_ORIGINS, built by buildCorsOrigins) across the CDK PlatformStack, GitHub Actions workflows, and Python backends — S3 bucket CORS for presigned uploads, local-dev CORS, and the origin allowlists for voice, OAuth callbacks and the SKIP_AUTH guard. Use when modifying allowed origins, adding a construct or check that needs them, debugging a CORS or "origin not allowed" error, considering removing CORS config after the BFF move, or touching any workflow env vars related to CDK_DOMAIN_NAME or CDK_CORS_ORIGINS.
---

# Allowed origins (`CORS_ORIGINS`)

## What it is for now

The deployed SPA calls app-api same-origin: `appApiUrl: '/api'` in
`environment.development.ts` / `environment.production.ts`, routed by
CloudFront's `/api/*` behavior to the app-api ALB. Same-origin is what lets the
BFF Token Handler's `__Host-` session cookies work, and it means the browser
never makes a cross-origin call to app-api in a deployed environment. It never
calls the AgentCore Runtime at all: app-api proxies chat to it
(`apis/app_api/chat/proxy_routes.py`).

So the list is less about CORS than its name suggests. It has four live jobs:

| Job | Where | Breaks without it |
|---|---|---|
| S3 bucket CORS for presigned uploads straight from the browser | `constructs/data/file-upload-construct.ts`, `constructs/rag/rag-data-construct.ts` (+ `spa/rag-cors-updater-construct.ts`), `constructs/fine-tuning/fine-tuning-data-construct.ts` | Uploads fail in the browser with a CORS error |
| Local dev: `ng serve` on `:4200` calls `http://localhost:8000` (`environment.ts`), a real cross-origin request | `CORSMiddleware` in `apis/app_api/main.py` | Every local request fails preflight |
| Origin allowlists that reuse the variable | voice WebSocket origin check (`apis/app_api/voice/routes.py`), `OAuth2CallbackUrl` validation (`apis/shared/middleware/agentcore_context.py`), the `SKIP_AUTH` guard in `apis/app_api/main.py` | Voice and MCP OAuth consent reject legitimate origins; the guard against `SKIP_AUTH` on a non-local deployment loses its input |
| `CORSMiddleware` on app-api and inference-api in deployed environments | `apis/app_api/main.py`, `apis/inference_api/main.py` | Nothing today — no browser calls them cross-origin. Harmless; keep it for local dev and for any future cross-origin client |

Do not remove or empty `CORS_ORIGINS` as "CORS cleanup after the BFF move": the
allowlists in the third row depend on it.

## How the list is built

1. `CDK_DOMAIN_NAME` → always applied as `https://{value}`, first
2. `CDK_CORS_ORIGINS` → additional global origins (optional, comma-separated)
3. Per-section `CDK_*_CORS_ORIGINS` → extras for one construct (optional)

localhost is never auto-included. Use `CDK_CORS_ORIGINS=http://localhost:4200`
for an environment local dev must reach.

```
CDK_DOMAIN_NAME → domainName → "https://{domainName}"  (always first)
CDK_CORS_ORIGINS → extraCorsOrigins                     (appended)
Result: config.corsOrigins = "https://{domainName},{extras}"
```

Every consumer calls `buildCorsOrigins(config, additionalOrigins?)` from
`infrastructure/lib/config.ts`, which splits, deduplicates and appends the
section extras:

```typescript
// Container env var (Fargate / AgentCore Runtime)
CORS_ORIGINS: buildCorsOrigins(config, config.appApi.additionalCorsOrigins).join(','),

// S3 bucket CORS rule
cors: [{ allowedOrigins: buildCorsOrigins(config, config.ragIngestion.additionalCorsOrigins) }]
```

The Python services read `CORS_ORIGINS` with no hardcoded fallback: an empty
value allows no origins.

Section extras exist on `AppApiConfig`, `InferenceApiConfig`,
`RagIngestionConfig` and `FineTuningConfig`. The file-upload bucket takes none
(`buildCorsOrigins(config)`), and neither does the SPA distribution, which
serves the app rather than calling anything cross-origin.

## Workflow requirements

`CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` go in the **job-level** `env:` block,
not workflow-level, because they read `vars.*`, which needs `environment:` on
the job (at workflow level they resolve to empty strings). Every workflow that
runs synth or deploy needs:

```yaml
env:
  CDK_DOMAIN_NAME: ${{ vars.CDK_DOMAIN_NAME }}
  CDK_CORS_ORIGINS: ${{ vars.CDK_CORS_ORIGINS }}
```

## Adding a consumer

First ask whether the browser really makes a cross-origin request to it. A new
app-api route doesn't: it is same-origin through `/api`. Something the browser
reaches directly — a new bucket with presigned uploads, a new WebSocket — does.

1. Import `buildCorsOrigins` from `infrastructure/lib/config.ts` (relative to the construct's folder under `lib/constructs/<area>/`)
2. Call `buildCorsOrigins(config, config.mySection.additionalCorsOrigins)`
3. If it needs its own extras: add `additionalCorsOrigins?: string` to the section's config interface and load it in `loadConfig()` from `CDK_MY_SECTION_CORS_ORIGINS`
4. Add `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` to the workflow job env
5. Add a test beside the existing `buildCorsOrigins` cases in `infrastructure/test/config.test.ts`

A new backend origin check reads `CORS_ORIGINS` at request time, as
`_allowed_callback_origins()` does, so tests can monkeypatch it.

## Common mistakes

- Putting `vars.*` in workflow-level `env:`
- Hardcoding `http://localhost:4200` in `buildCorsOrigins` or a Python fallback
- Using `config.domainName` directly instead of `buildCorsOrigins()`
- Setting `corsOrigins` in `cdk.context.json` (overrides domain derivation)
- Debugging a deployed "CORS error" on an app-api call: in a deployed environment it is almost always something else (a CloudFront behavior, a redirect to another origin, or a 5xx without CORS headers), because the call is same-origin
