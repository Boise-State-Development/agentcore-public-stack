# Project Structure

## Root Layout

```
agentcore-public-stack/
├── backend/                    # Python backend services
├── frontend/ai.client/         # Angular frontend application
├── infrastructure/             # AWS CDK infrastructure code
├── docs/                       # Documentation and specifications
├── scripts/                    # Deployment and build scripts
```

## Backend Structure

```
backend/
├── pyproject.toml              # Single source of truth for dependencies
├── venv/                       # Python virtual environment (gitignored)
├── src/
│   ├── agents/                 # Agent implementations
│   │   ├── main_agent/         # Primary conversational agent
│   │   │   ├── core/           # Agent factory, model config, system prompt
│   │   │   ├── session/        # Turn-based session management
│   │   │   │   ├── hooks/      # Prompt caching hooks
│   │   │   │   └── tests/      # Session manager tests
│   │   │   ├── streaming/      # SSE event processing & formatting
│   │   │   ├── tools/          # Tool registry & catalog
│   │   │   ├── multimodal/     # Image & document handling
│   │   │   ├── quota/          # Quota checking & enforcement
│   │   │   ├── integrations/   # External MCP & Gateway clients
│   │   │   └── utils/          # Global state, timezone utilities
│   │   ├── strands_agent/      # Alternative agent implementation
│   │   ├── builtin_tools/      # Code Interpreter, Browser tools
│   │   └── local_tools/        # Weather, search, visualization
│   └── apis/
│       ├── shared/             # Shared utilities across APIs
│       │   ├── auth/           # JWT validation, RBAC, dependencies
│       │   └── rbac/           # Role-based access control
│       ├── app_api/            # Main application API (port 8000)
│       │   ├── main.py         # FastAPI app entry point
│       │   ├── auth/           # Authentication routes
│       │   ├── sessions/       # Session management
│       │   ├── messages/       # Message handling
│       │   ├── files/          # File upload/download
│       │   ├── tools/          # Tool management
│       │   ├── assistants/     # Assistant configuration
│       │   ├── memory/         # Memory management
│       │   ├── costs/          # Cost tracking & aggregation
│       │   ├── users/          # User management
│       │   ├── admin/          # Admin endpoints (RBAC, quotas, tools)
│       │   ├── storage/        # DynamoDB & file storage
│       │   └── health/         # Health check endpoints
│       └── inference_api/      # Bedrock inference endpoint (port 8001)
│           ├── main.py         # FastAPI app entry point
│           ├── chat/           # Chat completion routes
│           └── health/         # Health check endpoints
└── tests/                      # pytest test suite
    ├── conftest.py             # Test fixtures
    └── agents/                 # Agent tests
```

## Frontend Structure

```
frontend/ai.client/
├── angular.json                # Angular CLI configuration
├── package.json                # npm dependencies
├── tsconfig.json               # TypeScript configuration
├── tailwind.config.js          # Tailwind CSS v4.1+ configuration
├── public/                     # Static assets
│   ├── favicon.ico
│   └── img/                    # Logo images
└── src/
    ├── main.ts                 # Application entry point
    ├── index.html              # HTML template
    ├── styles.css              # Global styles (Tailwind imports)
    ├── environments/           # Environment configurations
    │   ├── environment.ts
    │   ├── environment.development.ts
    │   └── environment.production.ts
    └── app/
        ├── app.ts              # Root component
        ├── app.routes.ts       # Route definitions
        ├── app.config.ts       # Application configuration
        ├── auth/               # Authentication module
        │   ├── login/          # Login component
        │   ├── callback/       # OAuth callback handler
        │   ├── auth.service.ts # Auth state management
        │   ├── auth.guard.ts   # Route guard
        │   └── auth.interceptor.ts # HTTP interceptor
        ├── session/            # Chat session module
        │   ├── session.component.ts
        │   ├── session.service.ts
        │   ├── message-list/   # Message display
        │   ├── message-input/  # User input
        │   └── tool-sidebar/   # Tool selection
        ├── admin/              # Admin dashboard
        │   ├── users/          # User management
        │   ├── costs/          # Cost dashboard
        │   ├── quota/          # Quota management
        │   ├── tools/          # Tool configuration
        │   └── roles/          # RBAC management
        ├── assistants/         # Assistant management
        ├── memory/             # Memory dashboard
        ├── files/              # File management
        ├── manage-sessions/    # Session list
        ├── components/         # Shared components
        │   ├── header/
        │   ├── sidebar/
        │   ├── tooltip/
        │   └── markdown/
        ├── services/           # Shared services
        │   ├── api.service.ts
        │   ├── sse.service.ts
        │   └── state.service.ts
        └── users/              # User profile
```

## Infrastructure Structure

```
infrastructure/
├── package.json                # npm dependencies
├── tsconfig.json               # TypeScript configuration
├── cdk.json                    # CDK configuration
├── cdk.context.json            # CDK context values
├── bin/
│   └── infrastructure.ts       # CDK app entry point
├── lib/
│   ├── config.ts               # Configuration loader & validator
│   ├── infrastructure-stack.ts # VPC, networking, DynamoDB tables
│   ├── app-api-stack.ts        # App API Fargate service
│   ├── inference-api-stack.ts  # Inference API Fargate service
│   ├── frontend-stack.ts       # CloudFront + S3 distribution
│   └── gateway-stack.ts        # API Gateway + Lambda functions
└── test/
    └── infrastructure.test.ts  # CDK stack tests
```

## Documentation Structure

```
docs/
├── specs/                      # Feature specifications
│   ├── ADMIN_COST_DASHBOARD_SPEC.md
│   ├── APP_ROLES_RBAC_SPEC.md
│   ├── FILE_UPLOAD_FEATURE_SPEC.md
│   ├── SESSION_DELETION_SPEC.md
│   ├── TOOL_RBAC_SPEC.md
│   └── USER_COST_TRACKING_SPEC.md
└── feature-summaries/          # Implementation summaries
    ├── ADMIN_COST_DASHBOARD_IMPLEMENTATION.md
    ├── FILE_UPLOAD_IMPLEMENTATION.md
    ├── MEMORY_DASHBOARD_IMPLEMENTATION.md
    ├── MULTIMODAL_FILE_ATTACHMENTS.md
    ├── QUOTA_IMPLEMENTATION_SUMMARY.md
    └── RBAC_IMPLEMENTATION.md
```

## Scripts Structure

```
scripts/
├── common/                     # Shared utilities
│   ├── install-deps.sh
│   └── load-env.sh
├── stack-app-api/              # App API deployment scripts
│   ├── build.sh
│   ├── deploy.sh
│   ├── push-to-ecr.sh
│   └── test.sh
├── stack-inference-api/        # Inference API deployment scripts
│   ├── build.sh
│   ├── deploy.sh
│   ├── push-to-ecr.sh
│   └── test.sh
├── stack-frontend/             # Frontend deployment scripts
│   ├── build.sh
│   ├── deploy-assets.sh
│   └── deploy-cdk.sh
├── stack-gateway/              # Gateway deployment scripts
│   ├── build-cdk.sh
│   └── deploy.sh
└── stack-infrastructure/       # Infrastructure deployment scripts
    ├── build.sh
    ├── deploy.sh
    └── synth.sh
```

## Key File Locations

### Configuration Files

- Backend dependencies: `backend/pyproject.toml`
- Backend environment: `backend/src/.env`
- Frontend dependencies: `frontend/ai.client/package.json`
- Frontend environment: `frontend/ai.client/src/environments/environment.ts`
- Infrastructure config: `infrastructure/cdk.context.json`
- CDK config: `infrastructure/lib/config.ts`

### Entry Points

- App API: `backend/src/apis/app_api/main.py`
- Inference API: `backend/src/apis/inference_api/main.py`
- Frontend: `frontend/ai.client/src/main.ts`
- CDK: `infrastructure/bin/infrastructure.ts`

### Important Modules

- Agent implementation: `backend/src/agents/main_agent/main_agent.py`
- Session management: `backend/src/agents/main_agent/session/turn_based_session_manager.py`
- Tool registry: `backend/src/agents/main_agent/tools/tool_registry.py`
- SSE streaming: `backend/src/agents/main_agent/streaming/stream_coordinator.py`
- RBAC: `backend/src/apis/shared/rbac/service.py`
- Auth service: `frontend/ai.client/src/app/auth/auth.service.ts`
- Chat component: `frontend/ai.client/src/app/session/session.component.ts`

## Naming Conventions

### Backend

- **Files**: snake_case (e.g., `turn_based_session_manager.py`)
- **Classes**: PascalCase (e.g., `TurnBasedSessionManager`)
- **Functions**: snake_case (e.g., `get_current_user`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_FILE_SIZE`)
- **Private**: Leading underscore (e.g., `_internal_method`)

### Frontend

- **Files**: kebab-case (e.g., `auth.service.ts`)
- **Components**: kebab-case (e.g., `message-list.component.ts`)
- **Classes**: PascalCase (e.g., `AuthService`)
- **Functions**: camelCase (e.g., `getCurrentUser`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `API_BASE_URL`)
- **Interfaces**: PascalCase with 'I' prefix optional (e.g., `User` or `IUser`)

### Infrastructure

- **Files**: kebab-case (e.g., `app-api-stack.ts`)
- **Classes**: PascalCase (e.g., `AppApiStack`)
- **Functions**: camelCase (e.g., `getResourceName`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `DEFAULT_REGION`)

## Module Organization

### Backend Imports

All modules are properly packaged and can be imported directly:

```python
# Shared utilities
from apis.shared.auth import get_current_user, User
from apis.shared.rbac import RBACService

# Agent modules
from agents.main_agent.main_agent import ChatbotAgent
from agents.main_agent.session import TurnBasedSessionManager
from agents.local_tools.weather import get_weather
```

### Frontend Imports

Use path aliases defined in `tsconfig.json`:

```typescript
// Services
import { AuthService } from '@app/auth/auth.service';
import { ApiService } from '@app/services/api.service';

// Components
import { MessageListComponent } from '@app/session/message-list/message-list.component';
```

## Build Artifacts

- **Frontend build**: `frontend/ai.client/dist/`
- **CDK synthesis**: `infrastructure/cdk.out/`
- **Python cache**: `**/__pycache__/`, `**/*.pyc`
- **Node modules**: `**/node_modules/`
- **Virtual env**: `backend/venv/`
- **Logs**: `*.log` (app_api.log, inference_api.log)
