# CLAUDE.md

Production-ready multi-agent conversational AI system built with AWS Bedrock AgentCore and Strands Agents.

## Build & Run

### Backend
```bash
cd backend
uv sync --extra agentcore --extra dev   # All deps
uv run python -m pytest tests/ -v       # Tests
cd src/apis/app_api && uv run python main.py    # App API (port 8000)
cd src/apis/inference_api && uv run python main.py  # Inference API (port 8001)
```

### Frontend
```bash
cd frontend/ai.client
npm install
npm run start    # Dev server (port 4200)
npm test         # Tests (Vitest via Analog.js)
```

### Infrastructure
```bash
cd infrastructure
npm install
npm run build
npx cdk synth
npx cdk deploy --all
```

## Key Conventions

- **Deploy order:** Infrastructure → Gateway → App API → Inference API → Frontend
- **Admin endpoints** go under `/admin/<domain>/`, user-facing under `/<domain>/`
- **Errors stream as assistant messages** via SSE (not HTTP error codes)
- **Signal-based state** throughout frontend (`signal()`, `computed()`)
- **All dependencies use exact version pins** — no `^`, `~`, or `>=`
- **Never install packages without explicit user approval**

## SSE Event Types

| Event | Purpose |
|-------|---------|
| `message_start` | Start of assistant response |
| `content_block_start/delta/stop` | Streaming content |
| `message_stop` | End of message |
| `tool_use` / `tool_result` | Tool invocation and result |
| `stream_error` | Conversational error |
| `done` | Stream complete |

## Multi-Protocol Tool Architecture

| Protocol | Location | Auth |
|----------|----------|------|
| Direct call | `agents/main_agent/tools/` | N/A |
| AWS SDK | `agents/main_agent/tools/` | IAM |
| MCP + SigV4 | Cloud Lambda (Gateway) | AWS SigV4 |
| A2A | Cloud Runtime | AgentCore auth |

## Cross-Package Contracts

- Backend route handlers define the API shape; frontend TypeScript interfaces must match
- Breaking changes to an endpoint require updating both packages in the same PR
- SSE event types are part of the contract — changes must be coordinated

## File Creation Rules

| Change Type | Where It Goes |
|-------------|---------------|
| New API route | `backend/src/apis/app_api/<domain>/` |
| New admin endpoint | `backend/src/apis/app_api/admin/<domain>/` |
| New agent tool | `backend/src/agents/main_agent/tools/` + register in `__init__.py` |
| New Angular page | `frontend/ai.client/src/app/<feature>/` |
| New CDK stack | `infrastructure/lib/<stack-name>-stack.ts` |
| Shared backend utils | `backend/src/apis/shared/` |

## Debugging Quick Reference

- **Tool not appearing:** Check `__init__.py` export, RBAC permissions, `enabled_tools`, ToolRegistry
- **Session not persisting:** Check AgentCore Memory config, session_id, TurnBasedSessionManager flush
- **SSE stream disconnecting:** Check 600s timeout, client connection, quota exceeded events

## Coding Standards

### Python
- `snake_case` functions/variables, `PascalCase` classes
- Type hints required on all function signatures
- No `print()` in production — use `logging`

### TypeScript
- `camelCase` functions/variables, `PascalCase` classes/interfaces, `UPPER_SNAKE_CASE` constants
- Strict mode enabled — no `any` unless absolutely necessary

### General
- No commented-out code in commits
- Prefer early returns over deep nesting
- One logical change per commit using conventional commits (`feat:`, `fix:`, `chore:`, etc.)

## Git Workflow

- Branch from `develop`, never `main`
- PRs target `develop`; `main` is updated via squash-merge releases only
- Branch naming: `feature/<short-description>`
