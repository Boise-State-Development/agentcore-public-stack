# Contributing to AgentCore Public Stack

## Prerequisites

- **Node.js** 20+ (for frontend and infrastructure)
- **Python** 3.13+ (for backend)
- **Docker** (for container builds)
- **AWS CLI** v2 (for cloud operations)
- **uv** (Python package manager — [install guide](https://docs.astral.sh/uv/getting-started/installation/))
- **npm** 11.2.0+ (for frontend/infrastructure dependencies)

## Clone and Install

```bash
git clone <repository-url>
cd agentcore-public-stack
```

### Backend

```bash
cd backend

# Install uv (one-time)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies (core + AgentCore + dev tools)
uv sync --extra agentcore --extra dev
```

### Frontend

```bash
cd frontend/ai.client
npm ci
```

### Infrastructure

```bash
cd infrastructure
npm ci
```

## Environment Configuration

### Backend

Copy and configure `backend/src/.env`:

```bash
AWS_REGION=us-west-2
AWS_PROFILE=<your-aws-profile>
AGENTCORE_MEMORY_ID=<memory-id>
AGENTCORE_PROJECT_PREFIX=<project-prefix>
```

See `backend/src/.env` for the full list of required variables (DynamoDB table names, S3 buckets, etc.).

### Frontend

Edit `frontend/ai.client/src/environments/environment.ts`:

```typescript
export const environment = {
  production: false,
  appApiUrl: 'http://localhost:8000',
  inferenceApiUrl: 'http://localhost:8001',
  enableAuthentication: true
};
```

## Running Tests

### Backend

```bash
cd backend
uv run python -m pytest tests/ -v
uv run python -m pytest tests/ -v --cov=src  # with coverage
```

### Frontend

```bash
cd frontend/ai.client
npm test
```

### Infrastructure

```bash
cd infrastructure
npx cdk synth  # validates CDK stacks compile and synthesize
```

## AWS Credentials

For local development, configure AWS credentials via one of:

1. AWS CLI profile: `aws configure --profile <profile-name>`
2. Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
3. IAM Identity Center (SSO): `aws sso login --profile <profile-name>`

Set `AWS_PROFILE` in `backend/src/.env` to match your configured profile. Note that `load_dotenv` uses `override=True`, so `.env` values take precedence over shell environment variables.

## Code Quality

```bash
# Backend
cd backend
uv run black src/          # formatting
uv run ruff check src/     # linting
uv run mypy src/           # type checking

# Frontend
cd frontend/ai.client
npx eslint src/            # linting
npx prettier --check src/  # formatting
```
