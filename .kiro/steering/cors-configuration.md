---
inclusion: fileMatch
fileMatchPattern: ["infrastructure/lib/*-stack.ts", "infrastructure/lib/config.ts", "infrastructure/test/cors*", "backend/src/apis/*/main.py", ".github/workflows/*.yml", "scripts/common/load-env.sh"]
---

# CORS Configuration

## Two-Layer Model

CORS origins are built from exactly two sources, applied consistently across every stack:

1. **CDK_DOMAIN_NAME** (required for production) — auto-applied as `https://{value}` to every CORS consumer. This is the primary domain of the application.
2. **CDK_CORS_ORIGINS** (optional) — additional origins appended globally. Use for localhost during local dev or extra domains.

localhost is NOT auto-included. For local development, set `CDK_CORS_ORIGINS=http://localhost:4200`.

## Per-Section Extras

Each stack can optionally append additional origins via section-specific env vars:

| Env Var | Stack | Config Field |
|---|---|---|
| `CDK_APP_API_CORS_ORIGINS` | App API | `appApi.additionalCorsOrigins` |
| `CDK_INFERENCE_API_CORS_ORIGINS` | Inference API | `inferenceApi.additionalCorsOrigins` |
| `CDK_FILE_UPLOAD_CORS_ORIGINS` | Infrastructure (file upload S3) | `fileUpload.additionalCorsOrigins` |
| `CDK_RAG_CORS_ORIGINS` | RAG Ingestion (S3) | `ragIngestion.additionalCorsOrigins` |
| `CDK_ASSISTANTS_CORS_ORIGINS` | Assistants | `assistants.additionalCorsOrigins` |
| `CDK_FINE_TUNING_CORS_ORIGINS` | SageMaker Fine-Tuning (S3) | `fineTuning.additionalCorsOrigins` |

## Shared Helper

All stacks use `buildCorsOrigins(config, additionalOrigins?)` from `config.ts`:

```typescript
import { buildCorsOrigins } from './config';

// Global origins only (domain + CDK_CORS_ORIGINS)
const origins = buildCorsOrigins(config);

// Global + section-specific extras
const origins = buildCorsOrigins(config, config.ragIngestion.additionalCorsOrigins);
```

For container env vars (Fargate, AgentCore Runtime):
```typescript
CORS_ORIGINS: buildCorsOrigins(config, config.appApi.additionalCorsOrigins).join(','),
```

For S3 bucket CORS rules:
```typescript
cors: [{ allowedOrigins: buildCorsOrigins(config, config.fileUpload?.additionalCorsOrigins) }]
```

## Python Backend

Both FastAPI apps read `CORS_ORIGINS` env var (set by CDK):

```python
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in _cors_origins if o.strip()])
```

## Flow

```
GitHub vars.CDK_DOMAIN_NAME + vars.CDK_CORS_ORIGINS
  → workflow job-level env (MUST be job-level, not workflow-level)
  → scripts/common/load-env.sh (--context domainName, --context corsOrigins)
  → infrastructure/lib/config.ts (corsOrigins = "https://{domainName}" + extras)
  → buildCorsOrigins(config, sectionExtras?) → string[]
  → S3 CORS rules / container CORS_ORIGINS env var
```

## Critical Rules

- **NEVER** put `vars.*` in workflow-level `env:` — they resolve to empty strings. Always use job-level `env:` on jobs with `environment:` set.
- **NEVER** hardcode localhost or `*` as CORS origins.
- **EVERY** workflow that runs `synth` or `deploy` MUST have `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` in its job-level env.
- **EVERY** new stack that needs CORS must use `buildCorsOrigins()`.
