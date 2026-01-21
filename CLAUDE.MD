# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Production-ready multi-agent conversational AI system built with AWS Bedrock AgentCore and Strands Agents.

**Tech Stack:**
- **Frontend**: Angular v21, TypeScript, Tailwind v4.1+ CSS
- **Backend**: Python 3.13+, FastAPI
- **Agent Framework**: Strands Agents (AWS Bedrock)
- **Cloud Services**: AWS Bedrock AgentCore (Runtime, Memory, Gateway, Code Interpreter, Browser)

## Build and Run Commands

```bash
# Full setup (one-time)
./setup.sh

# Start all services locally
./start.sh
```

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate

# Install for app_api only
pip install -e "."

# Install for inference_api (with AgentCore)
pip install -e ".[agentcore]"

# Install all including dev tools
pip install -e ".[agentcore,dev]"

# Run App API (port 8000)
cd src/apis/app_api && python main.py

# Run Inference API (port 8001)
cd src/apis/inference_api && python main.py

# Run tests
python -m pytest tests/ -v
```

### Frontend

```bash
cd frontend/ai.client
npm install
npm run start        # Dev server (port 4200)
npm run build        # Production build
npm test             # Run tests (Vitest)
```

### Infrastructure (CDK)

```bash
cd infrastructure
npm install
npm run build        # Compile TypeScript
npx cdk synth        # Generate CloudFormation
npx cdk deploy --all # Deploy all stacks
npx cdk diff         # Compare changes
```

## Project Structure

```
/
├── backend/
│   └── src/
│       ├── agents/main_agent/          # Core agent implementation
│       │   ├── core/                   # Agent factory, model config, system prompt
│       │   ├── session/                # Turn-based session management
│       │   ├── streaming/              # SSE event processing & formatting
│       │   └── tools/                  # Tool registry & local tools
│       └── apis/
│           ├── app_api/                # Main application API (port 8000)
│           ├── inference_api/          # Bedrock inference endpoint (port 8001)
│           └── shared/                 # Shared utilities & error handling
├── frontend/ai.client/                 # Angular SPA
│   └── src/app/
│       ├── auth/                       # OIDC/Entra ID authentication
│       ├── session/                    # Chat UI & services
│       ├── admin/                      # Admin dashboard pages
│       └── services/                   # State management, HTTP services
└── infrastructure/                     # AWS CDK infrastructure
    └── lib/                            # Stack definitions
```

## Key Architecture Patterns

### Multi-Protocol Tool Architecture

| Protocol | Location | Examples | Auth |
|----------|----------|----------|------|
| Direct call | `agents/main_agent/tools/` | Weather, Calculator | N/A |
| AWS SDK | `agents/main_agent/tools/` | Code Interpreter, Browser | IAM |
| MCP + SigV4 | Cloud Lambda (Gateway) | Wikipedia, ArXiv, Finance | AWS SigV4 |
| A2A | Cloud Runtime (WIP) | Report Writer | AgentCore auth |

### Turn-based Session Management

**File:** `backend/src/agents/main_agent/session/turn_based_session_manager.py`

Messages are buffered within a turn to reduce AgentCore Memory API calls:
- Pass-through mode: Messages sent individually
- Message count initialized once at startup
- Supports session cancellation via `cancelled` flag

### Prompt Caching Strategy

**Files:**
- `backend/src/agents/main_agent/core/model_config.py`
- `backend/src/agents/main_agent/session/hooks/conversation_caching.py`

Three-level caching (Bedrock only): System Prompt → Tool Definitions → Conversation History

### SSE Event Types

| Event | Purpose |
|-------|---------|
| `message_start` | Start of assistant response |
| `content_block_start/delta/stop` | Streaming content |
| `message_stop` | End of message |
| `tool_use` | Tool invocation |
| `tool_result` | Tool execution result |
| `stream_error` | Conversational error |
| `done` | Stream complete |

## Backend API Route Conventions

All admin endpoints MUST be under the `/admin/` prefix:

| Pattern | Example | Use For |
|---------|---------|---------|
| `/resource/` | `/tools/` | User-facing endpoints |
| `/admin/resource/` | `/admin/tools/` | Admin CRUD |

**Correct:** `GET /admin/tools/`, `PUT /admin/tools/{id}`
**Incorrect:** ~~`GET /tools/admin/`~~

## Authentication Flow

**Frontend:** OIDC with Entra ID + PKCE

1. `login()` → Backend `/auth/login` → Entra ID authorization
2. Callback → `/auth/token` exchange → Store tokens in localStorage
3. `authInterceptor` adds Bearer token, auto-refreshes on expiry
4. `authGuard` protects routes

## Error Handling

Errors stream as assistant messages (better UX than HTTP errors):
- `ConversationalErrorEvent` for user-friendly markdown errors
- Errors persisted to session history
- Fail-open approach for RBAC/quota errors

## Frontend Guidelines

### Icon-Only Buttons (Accessibility Required)

Buttons with only icons MUST include a tooltip:

```html
<button
  (click)="editItem()"
  class="p-2 text-gray-500 hover:text-blue-600"
  [appTooltip]="'Edit Item'"
  appTooltipPosition="top"
>
  <ng-icon name="heroPencilSquare" class="size-5" />
</button>
```

### State Management

Signal-based state throughout (`signal()`, `computed()`)

## Tailwind CSS v4.1+ Rules

See `.cursor/rules/tailwind.mdc` for comprehensive guidelines. Key points:

### Breaking Changes (ALWAYS use v4 syntax)

| ❌ Deprecated | ✅ Replacement |
|---------------|----------------|
| `bg-opacity-*` | `bg-black/50` |
| `bg-gradient-*` | `bg-linear-*` |
| `shadow-sm` | `shadow-xs` |
| `shadow` | `shadow-sm` |
| `rounded-sm` | `rounded-xs` |
| `rounded` | `rounded-sm` |
| `leading-*` | `text-base/7` (line-height modifier) |
| `space-x-*` in flex | `gap-*` |

### Layout Rules

- Always use `gap-*` in flex/grid, never `space-x-*` or `space-y-*`
- Use `min-h-dvh` instead of `min-h-screen` (mobile Safari bug)
- Use `size-*` over separate `w-*` and `h-*` for equal dimensions
- Never use `@apply`

## CDK Infrastructure

**Deploy order:** InfrastructureStack → AppApiStack → InferenceApiStack → FrontendStack → GatewayStack

### DynamoDB Tables (AppApiStack)

| Table | Purpose |
|-------|---------|
| `user-quotas` | Quota tier assignments |
| `quota-events` | Quota usage events |
| `sessions-metadata` | Message-level cost tracking |
| `user-cost-summary` | Aggregated user costs |
| `managed-models` | Model pricing/config |
| `users` | User profiles from JWT |
| `app-roles` | RBAC role definitions |

## Environment Configuration

### Backend (.env)

```bash
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
AGENTCORE_MEMORY_ID=<memory-id>
AGENTCORE_RUNTIME_ID=<runtime-id>
```

### Frontend (environment.ts)

```typescript
export const environment = {
  production: false,
  appApiUrl: 'http://localhost:8000',
  inferenceApiUrl: 'http://localhost:8001',
  enableAuthentication: true
};
```

## Debugging

**Tool not appearing:** Check export in `__init__.py`, RBAC permissions, `enabled_tools` list, ToolRegistry registration

**Session not persisting:** Check AgentCore Memory config, session_id, TurnBasedSessionManager flush

**SSE stream disconnecting:** Check 600-second timeout, client connection, quota exceeded events

## Constraints

1. Only implement what's explicitly requested
2. Watch for injection vulnerabilities (command, XSS, SQL)
3. Consider token usage when modifying agent prompts
4. Maintain multimodal compatibility
5. Respect turn-based buffering patterns
6. Use correct protocol for tool type
7. Errors should stream as conversational messages

## Known Limitations

- AgentCore Gateway requires cloud deployment (not available locally)
- AgentCore Memory uses local file storage in development
- A2A Report Writer tools are work in progress
- Browser automation requires Nova Act model access
- Stream timeout: 600 seconds
