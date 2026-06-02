# Copilot Instructions — AgentCore Public Stack

Production multi-agent conversational AI platform built on AWS Bedrock AgentCore + Strands Agents. Monorepo with four top-level packages: `backend/` (Python 3.13, FastAPI), `frontend/ai.client/` (Angular 21 + Analog.js), `infrastructure/` (AWS CDK, TypeScript), and `scripts/`.

Authoritative deeper docs: `CLAUDE.MD` (architecture), `CONTRIBUTING.md` (setup), `.kiro/steering/` and `.claude/skills/` (topic-specific patterns — CDK, Tailwind, Angular signals, CORS, release notes, versioning).

## Build, Test, Lint

### Backend (`cd backend`)
```bash
uv sync --extra agentcore --extra dev
uv run python -m pytest tests/ -v
uv run python -m pytest tests/path/to/test_file.py::test_name -v   # single test
uv run black src/ && uv run ruff check src/ && uv run mypy src/
# Run services locally:
cd src/apis/app_api && uv run python main.py        # port 8000
cd src/apis/inference_api && uv run python main.py  # port 8001
```

### Frontend (`cd frontend/ai.client`)
```bash
npm ci
npm run start                          # dev server on 4200
npm test                               # Vitest via Analog.js
npx vitest run path/to/file.spec.ts    # single test file
npx eslint src/ && npx prettier --check src/
```

### Infrastructure (`cd infrastructure`)
```bash
npm ci && npx tsc --noEmit
npx jest                                          # CDK construct + stack tests
npx cdk synth                                     # validates the stack
npx cdk deploy {prefix}-PlatformStack             # deploy the single stack
```

## Architecture — the big picture

- **Three independent backend consumers** of `apis.shared`: `app_api`, `inference_api`, and `agents/`. They must **never import from each other** — only from `apis.shared`. Enforced by `backend/tests/architecture/test_import_boundaries.py`.
- **Inference API runs inside an AgentCore Runtime container.** The runtime data plane only proxies `POST /invocations` and `GET /ping` — any other route returns 404 in cloud (works locally because `localhost:8001` bypasses the gateway). User-facing CRUD endpoints **belong in app-api**, not inference-api. To get workload context on app-api, use the `AGENTCORE_RUNTIME_WORKLOAD_NAME` mint fallback in `apis/shared/oauth/agentcore_identity.py`.
- **Single CDK stack.** All AWS resources live in one `PlatformStack` (`infrastructure/lib/platform-stack.ts`). No cross-stack SSM references between CDK stacks; values that flow between constructs go through the typed `PlatformComputeRefs` interface.
- **Deploy order:** `platform.yml` (CDK, only when infra changes) → `backend.yml` (per-image build + AWS-API code deploy: app-api, inference-api, rag-ingestion, artifact-render in parallel) → `frontend-deploy.yml` (S3 sync + CloudFront invalidation). Day-to-day code changes only re-run `backend.yml`. Compute image URIs are read from SSM at CFN deploy time (`/{prefix}/app-api/image-tag`, `/{prefix}/inference-api/image-tag`) so any task-def or Runtime property change picks up whatever image the build pipeline most recently pushed.
- **Errors stream as assistant messages over SSE**, not HTTP error codes. See SSE event table in `CLAUDE.MD` (`message_start`, `content_block_*`, `tool_use`/`tool_result`, `ui_resource`, `stream_error`, `oauth_required`, `compaction`, `done`).
- **Multi-protocol tools:** direct/AWS-SDK tools live in `agents/main_agent/tools/`; remote tools come via MCP+SigV4 (Gateway Lambda) or A2A (Runtime). A2A is currently **client-only**; if exposing an A2A server, `capabilities` must include `streaming=True` or clients hang.
- **Frontend is signal-based** throughout (`signal()`, `computed()`). API shapes are defined by backend routes; matching TS interfaces must be updated in the same PR as breaking backend changes.

## Conventions specific to this repo

- **Auth on `apis/app_api/` routes** uses `Depends(get_current_user_from_session)` (cookie-based) or `Depends(require_admin)`. The SPA sends an httpOnly session cookie, **not** `Authorization: Bearer`. Bearer-only deps on user-facing routes cause a 401 → redirect loop. Exceptions: `auth/api_keys/` (X-API-Key) and `voice/` (voice-ticket cookie) — do not template off these.
- **Admin endpoints** go under `/admin/<domain>/`, user-facing under `/<domain>/`.
- **Exact dependency pins only** — no `^`, `~`, or `>=` anywhere (Python, npm, CDK).
- **Never install new packages without explicit user approval.**
- **Branch from `develop`**, never `main`. PRs target `develop`; `main` advances only via squash-merge releases. Branch naming: `feature/<short-description>`. Sign commits with `git commit -s` (DCO).
- **Conventional commits** (`feat:`, `fix:`, `chore:`, ...), one logical change per commit.
- **No `print()` in backend** — use `logging`. Python: `snake_case` / `PascalCase`, type hints required. TS: strict mode, no `any` unless unavoidable.

## File placement

| Change | Location |
|---|---|
| New API route | `backend/src/apis/app_api/<domain>/` |
| Admin endpoint | `backend/src/apis/app_api/admin/<domain>/` |
| New agent tool | `backend/src/agents/main_agent/tools/` + register in `__init__.py` |
| Shared backend code | `backend/src/apis/shared/<domain>/` |
| Lambda for an infra stack | `backend/src/lambdas/<lambda-name>/` (not part of `apis/` boundary) |
| Angular page | `frontend/ai.client/src/app/<feature>/` |
| New CDK construct | `infrastructure/lib/constructs/<area>/<name>-construct.ts` — compose into `PlatformStack` (`lib/platform-stack.ts`); if it exposes values to compute constructs, thread them through `PlatformComputeRefs` rather than SSM. There are no separate CDK *stacks* anymore. |

## Debugging cheatsheet

- **Tool not appearing:** check `__init__.py` export, RBAC permissions, `enabled_tools`, ToolRegistry.
- **Session not persisting:** check AgentCore Memory config, `session_id`, `TurnBasedSessionManager` flush.
- **SSE stream disconnecting:** check the 600s timeout, client connection, quota-exceeded events.
- **Local inference-api route works, cloud returns 404:** the route isn't `/invocations` or `/ping` — move it to app-api (see Architecture).

## Topic deep-dives

Before non-trivial work in these areas, consult the matching skill/steering doc:

- CDK stacks/constructs → `.claude/skills/cdk-infrastructure/` and `.kiro/steering/cdk-*.md`
- Angular components/signals → `.claude/skills/angualar-best-practices/` and `.kiro/steering/angular-*.md`
- Tailwind v4 / a11y → `.claude/skills/tailwind-ui/` and `.kiro/steering/tailwind-*.md`
- CORS across stacks → `.claude/skills/cors-deployment/SKILL.md`
- Release notes / CHANGELOG → `.claude/skills/release-notes/SKILL.md`
- Version bumps → `.claude/skills/versioning/SKILL.md`
