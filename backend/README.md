# AgentCore Backend

This backend uses a unified dependency management approach with a single `pyproject.toml` file, managed by [uv](https://docs.astral.sh/uv/) for fast, reproducible, and secure dependency resolution.

## Installation

### Prerequisites

Install `uv` (one-time setup):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### For App API (Authentication Service)

The App API only needs core dependencies (FastAPI, auth utilities):

```bash
cd backend
uv sync
```

### For Inference API (Agent Execution Service)

The Inference API needs core dependencies + AgentCore-specific packages:

```bash
cd backend
uv sync --extra agentcore
```

### For Development (All Dependencies + Dev Tools)

Install everything including pytest, black, ruff, mypy:

```bash
cd backend
uv sync --extra agentcore --extra dev
```

> **Note:** `uv sync` creates and manages a `.venv` automatically. You don't need to create or activate a virtual environment manually. Use `uv run` to execute commands inside the managed environment.

## AWS Configuration

This project requires AWS credentials for Bedrock and other AWS services.

**Quick Setup:**

```bash
# Configure AWS CLI profile
aws configure --profile my-profile

# Set in .env file
echo "AWS_PROFILE=my-profile" >> src/.env

# Or use environment variable
export AWS_PROFILE=my-profile
```

## Project Structure

```
backend/
├── pyproject.toml          # Single source of truth for all dependencies
├── uv.lock                 # Locked dependency graph (committed to git)
├── .venv/                  # Virtual environment (managed by uv, gitignored)
├── src/
│   ├── agents/             # Agent implementations
│   └── apis/
│       ├── shared/         # Shared auth utilities
│       ├── app_api/        # Authentication API (port 8000)
│       └── inference_api/  # Agent execution API (port 8001)
```

## Running APIs

### App API (Authentication)
```bash
cd backend
cd src/apis/app_api
uv run python main.py
# Runs on http://localhost:8000
```

### Inference API (Agent Execution)
```bash
cd backend
cd src/apis/inference_api
uv run python main.py
# Runs on http://localhost:8001
```

## Authentication Providers

The platform supports configurable OIDC authentication providers, allowing admins to set up one or more identity providers (Entra ID, AWS Cognito, Okta, Google, etc.) through the Admin UI.

### First-Time Setup (Bootstrap)

When deploying for the first time, no auth providers exist yet and no users can log in — a classic chicken-and-egg problem. The **seed script** solves this by writing directly to DynamoDB and Secrets Manager, bypassing the API entirely.

---

### Step 1: Prerequisites

Before running the seed script, ensure the following are in place:

**1a. Deploy CDK infrastructure**

The CDK stacks must be deployed first. This creates the DynamoDB `auth-providers` table, the Secrets Manager secret for client secrets, and the OIDC state table.

```bash
cd infrastructure
npm install
npx cdk deploy --all
```

After deployment, note the following resource names from the CDK outputs or AWS Console:

| Resource | Where to Find | Example Value |
|----------|---------------|---------------|
| Auth providers table name | CDK output or SSM: `/{prefix}/auth/auth-providers-table-name` | `my-app-auth-providers-dev` |
| Auth provider secrets ARN | CDK output or SSM: `/{prefix}/auth/auth-provider-secrets-arn` | `arn:aws:secretsmanager:us-west-2:123456789:secret:my-app-auth-provider-secrets-dev-AbCdEf` |
| OIDC state table name | CDK output or SSM: `/{prefix}/auth/oidc-state-table-name` | `my-app-oidc-state-dev` |

**1b. AWS credentials configured**

Ensure AWS credentials are available via env vars, AWS profile, or IAM role. The seed script needs permissions to write to DynamoDB and Secrets Manager.

```bash
# Option A: AWS profile
export AWS_PROFILE=my-profile

# Option B: Environment variables
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2
```

**1c. Dependencies installed**

```bash
cd backend
uv sync
```

**1d. Register your application with your OIDC provider**

Before running the seed script, you need a Client ID and Client Secret from your identity provider. You will also need to know which JWT claim your provider uses for roles so you can configure system admin access via the bootstrap seed script.

<details>
<summary><strong>Microsoft Entra ID setup</strong></summary>

1. Go to **Azure Portal > Microsoft Entra ID > App registrations > New registration**
2. Set the **Redirect URI** to `http://localhost:4200/auth/callback` (Web platform)
3. After creation, copy the **Application (client) ID** and **Directory (tenant) ID**
4. Go to **Certificates & secrets > New client secret**, copy the secret value
5. Go to **API permissions > Add a permission > Microsoft Graph > Delegated** and ensure `openid`, `profile`, `email`, and `offline_access` are granted
6. Go to **Expose an API > Add a scope**:
   - Set the **Application ID URI** (defaults to `api://{CLIENT_ID}`)
   - Add a scope named `Read` (full value: `api://{CLIENT_ID}/Read`)
   - This custom scope is required by the platform to obtain a properly-scoped access token
7. Go to **Token configuration > Add optional claim**, add `email`, `given_name`, `family_name` to the ID token
8. Go to **App roles** to create application roles (e.g., `Admin`, `User`). Assign users to these roles in **Enterprise applications > Users and groups**
9. Your **Issuer URL** is: `https://login.microsoftonline.com/{TENANT_ID}/v2.0`
10. Entra ID emits roles in the `roles` claim by default

</details>

<details>
<summary><strong>AWS Cognito setup</strong></summary>

1. Go to **AWS Console > Amazon Cognito > Create user pool**
2. Configure sign-in options (email, username, etc.)
3. Under **App integration > App clients**, create an app client:
   - Note the **Client ID** and **Client secret**
   - Set **Allowed callback URLs** to `http://localhost:4200/auth/callback`
   - Set **Allowed sign-out URLs** to `http://localhost:4200`
   - Enable **Authorization code grant** flow
   - Set **OpenID Connect scopes**: `openid`, `profile`, `email`
4. Under **App integration**, note your **Cognito domain** (e.g., `https://my-app.auth.us-west-2.amazoncognito.com`)
5. To enable refresh tokens, go to **App client settings > Auth Flows** and ensure **Refresh token** is enabled (it is by default in Cognito)
6. Your **Issuer URL** is: `https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}`
7. To use roles with Cognito, create a **custom attribute** or use **Cognito groups**. Groups are emitted in the `cognito:groups` claim by default

</details>

---

### Step 2: Run the Seed Script

The seed script supports both interactive mode (prompts for each value) and non-interactive mode (all values passed as flags).

**Interactive mode** (prompts for all values):

```bash
cd backend
uv run python scripts/seed_auth_provider.py
```

**Dry run** (preview what would be written without making changes):

```bash
uv run python scripts/seed_auth_provider.py --dry-run \
    --provider-id entra-id \
    --issuer-url "https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0" \
    --client-id "YOUR_CLIENT_ID" \
    --display-name "Microsoft Entra ID" \
    --scopes "openid profile email api://YOUR_CLIENT_ID/Read offline_access" \
    --table-name my-app-auth-providers-dev \
    --secrets-arn "arn:aws:secretsmanager:us-west-2:123456789:secret:my-app-auth-provider-secrets-dev-AbCdEf" \
    --discover
```

#### Example: Microsoft Entra ID (non-interactive)

```bash
uv run python scripts/seed_auth_provider.py \
    --provider-id entra-id \
    --display-name "Microsoft Entra ID" \
    --issuer-url "https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0" \
    --client-id "YOUR_ENTRA_CLIENT_ID" \
    --discover \
    --scopes "openid profile email api://YOUR_ENTRA_CLIENT_ID/Read offline_access" \
    --user-id-claim "sub" \
    --email-claim "email" \
    --name-claim "name" \
    --roles-claim "roles" \
    --first-name-claim "given_name" \
    --last-name-claim "family_name" \
    --picture-claim "picture" \
    --button-color "#0078D4" \
    --table-name my-app-auth-providers-dev \
    --secrets-arn "arn:aws:secretsmanager:us-west-2:123456789:secret:my-app-auth-provider-secrets-dev-AbCdEf" \
    --region us-west-2
```

> **Note on Entra ID scopes:** The `api://{CLIENT_ID}/Read` scope is an application-specific scope registered under **Expose an API** in your Entra ID app registration. It ensures the access token is scoped to your application. The `offline_access` scope is required to obtain a refresh token, which enables the frontend to silently refresh expired sessions without re-prompting the user to log in.

The script will securely prompt for the client secret since `--client-secret` was omitted (recommended to keep it out of shell history).

#### Example: AWS Cognito (non-interactive)

```bash
uv run python scripts/seed_auth_provider.py \
    --provider-id cognito \
    --display-name "AWS Cognito" \
    --issuer-url "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_AbCdEfGhI" \
    --client-id "YOUR_COGNITO_CLIENT_ID" \
    --discover \
    --scopes "openid profile email" \
    --user-id-claim "sub" \
    --email-claim "email" \
    --name-claim "cognito:username" \
    --roles-claim "cognito:groups" \
    --first-name-claim "given_name" \
    --last-name-claim "family_name" \
    --button-color "#FF9900" \
    --table-name my-app-auth-providers-dev \
    --secrets-arn "arn:aws:secretsmanager:us-west-2:123456789:secret:my-app-auth-provider-secrets-dev-AbCdEf" \
    --region us-west-2
```

> **Note on Cognito scopes:** Cognito issues refresh tokens by default when the `openid` scope is requested — you do not need to add `offline_access` explicitly. Cognito does not support the `offline_access` scope as a request parameter. Refresh token behavior is controlled through the app client settings in the Cognito console (token expiration, rotation, etc.).

**Key flags:**

| Flag | Purpose |
|------|---------|
| `--discover` | Auto-fills all OIDC endpoints from the provider's `.well-known/openid-configuration` |
| `--client-secret` | Optional on the command line — if omitted, prompted securely via password input |
| `--dry-run` | Preview the DynamoDB item that would be written without making changes |
| `--force` | Overwrite an existing provider without prompting for confirmation |
| `--profile` | Use a specific AWS CLI profile |

Run `uv run python scripts/seed_auth_provider.py --help` for the full list of options.

---

### Step 3: Configure Environment Variables

After the seed script completes, ensure these variables are set in your backend `.env` file (at `backend/src/.env`) or in your ECS task definition for deployed environments:

```bash
# --- Auth Provider Management ---
# DynamoDB table name for auth provider configurations (from CDK output)
DYNAMODB_AUTH_PROVIDERS_TABLE_NAME=my-app-auth-providers-dev

# Secrets Manager ARN where client secrets are stored (from CDK output)
AUTH_PROVIDER_SECRETS_ARN=arn:aws:secretsmanager:us-west-2:123456789:secret:my-app-auth-provider-secrets-dev-AbCdEf

# DynamoDB table for OIDC login state (prevents CSRF, from CDK output)
DYNAMODB_OIDC_STATE_TABLE_NAME=my-app-oidc-state-dev

# --- Authentication Toggle ---
# Must be true for OIDC auth to be active
ENABLE_AUTHENTICATION=true
```

**About system admin access:** System administrator access is configured via the bootstrap seed script, which maps a JWT role to the `system_admin` AppRole in DynamoDB. Set the `SEED_ADMIN_JWT_ROLE` GitHub variable (e.g., `Admin`) and run the "Seed Bootstrap Data" workflow. When a user logs in, the backend resolves their JWT roles against AppRole mappings. System admins can manage auth providers, RBAC roles, models, and quotas through the Admin UI. Make sure at least one user in your IdP is assigned the configured admin role.

---

### Step 4: Start the Backend and Log In

```bash
cd backend/src/apis/app_api && uv run python main.py
```

1. Open `http://localhost:4200` in your browser
2. The login page will display your configured provider
3. Click the provider button and authenticate through your IdP
4. If your JWT contains a role mapped to the `system_admin` AppRole (configured via the bootstrap seed script), you will have admin access to manage providers, roles, models, and quotas through the Admin UI

---

### Auth Provider Model Field Reference

The table below describes every field in the OIDC auth provider model, its purpose, and sample values for both Entra ID and Cognito.

#### Identity & Core Configuration

| Field | CLI Flag | Required | Description | Entra ID Example | Cognito Example |
|-------|----------|----------|-------------|------------------|-----------------|
| `provider_id` | `--provider-id` | Yes | Unique slug identifier. Lowercase letters, numbers, and hyphens only. Used as the key in DynamoDB and Secrets Manager. | `entra-id` | `cognito` |
| `display_name` | `--display-name` | Yes | Human-readable name shown on the login page button. | `Microsoft Entra ID` | `AWS Cognito` |
| `provider_type` | — | No | Protocol type. Currently only `oidc` is supported. | `oidc` | `oidc` |
| `enabled` | `--enabled` | No | Whether this provider appears on the login page. Defaults to `true`. | `true` | `true` |

#### OIDC Endpoints

These are auto-discovered when using the `--discover` flag. You can override any of them manually.

| Field | CLI Flag | Required | Description | Entra ID Example | Cognito Example |
|-------|----------|----------|-------------|------------------|-----------------|
| `issuer_url` | `--issuer-url` | Yes | The OIDC issuer URL. Used to discover endpoints and validate JWT `iss` claims. | `https://login.microsoftonline.com/{TENANT_ID}/v2.0` | `https://cognito-idp.us-west-2.amazonaws.com/us-west-2_AbCdEfGhI` |
| `authorization_endpoint` | `--authorization-endpoint` | No* | URL where users are redirected to authenticate. Auto-discovered from `.well-known/openid-configuration`. | `https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize` | `https://my-app.auth.us-west-2.amazoncognito.com/oauth2/authorize` |
| `token_endpoint` | `--token-endpoint` | No* | URL used to exchange the authorization code for tokens. | `https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token` | `https://my-app.auth.us-west-2.amazoncognito.com/oauth2/token` |
| `jwks_uri` | `--jwks-uri` | No* | URL to fetch the provider's JSON Web Key Set for JWT signature verification. | `https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys` | `https://cognito-idp.us-west-2.amazonaws.com/us-west-2_AbCdEfGhI/.well-known/jwks.json` |
| `userinfo_endpoint` | `--userinfo-endpoint` | No | URL to fetch additional user profile information. | `https://graph.microsoft.com/oidc/userinfo` | `https://my-app.auth.us-west-2.amazoncognito.com/oauth2/userInfo` |
| `end_session_endpoint` | `--end-session-endpoint` | No | URL to redirect users for logout. | `https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/logout` | `https://my-app.auth.us-west-2.amazoncognito.com/logout` |

\* Auto-discovered with `--discover`. Required if not using discovery.

#### OAuth Configuration

| Field | CLI Flag | Required | Description | Entra ID Example | Cognito Example |
|-------|----------|----------|-------------|------------------|-----------------|
| `client_id` | `--client-id` | Yes | The OAuth application/client ID registered with the IdP. | `a1b2c3d4-e5f6-7890-abcd-ef1234567890` | `1a2b3c4d5e6f7g8h9i0j1k2l3` |
| `client_secret` | `--client-secret` | Yes | The OAuth client secret. Stored securely in AWS Secrets Manager, never in DynamoDB. Prompted via secure input if omitted from CLI. | *(secret value)* | *(secret value)* |
| `scopes` | `--scopes` | No | Space-separated OAuth scopes to request. Defaults to `openid profile email`. Include `offline_access` for providers that require it to issue refresh tokens (e.g., Entra ID). Include provider-specific API scopes if required (e.g., `api://{CLIENT_ID}/Read` for Entra ID). Cognito issues refresh tokens automatically and does not need `offline_access`. | `openid profile email api://{CLIENT_ID}/Read offline_access` | `openid profile email` |
| `response_type` | — | No | OAuth response type. Always `code` (authorization code flow). | `code` | `code` |
| `pkce_enabled` | `--pkce-enabled` | No | Whether to use PKCE (Proof Key for Code Exchange) for added security. Defaults to `true`. | `true` | `true` |
| `redirect_uri` | `--redirect-uri` | No | Override for the OAuth redirect URI. If not set, the backend constructs it automatically. | `http://localhost:4200/auth/callback` | `http://localhost:4200/auth/callback` |

#### JWT Claim Mappings

These tell the platform which JWT claims to read for user identity. Different IdPs use different claim names.

| Field | CLI Flag | Default | Description | Entra ID Value | Cognito Value |
|-------|----------|---------|-------------|----------------|---------------|
| `user_id_claim` | `--user-id-claim` | `sub` | JWT claim containing the unique user identifier. | `sub` | `sub` |
| `email_claim` | `--email-claim` | `email` | JWT claim containing the user's email address. | `email` | `email` |
| `name_claim` | `--name-claim` | `name` | JWT claim containing the user's display name. | `name` | `cognito:username` |
| `roles_claim` | `--roles-claim` | `roles` | JWT claim containing the user's roles/groups array. Used by the AppRole system to resolve user permissions. | `roles` | `cognito:groups` |
| `picture_claim` | `--picture-claim` | `picture` | JWT claim containing the user's profile picture URL. | `picture` | *(not available by default)* |
| `first_name_claim` | `--first-name-claim` | `given_name` | JWT claim containing the user's first name. | `given_name` | `given_name` |
| `last_name_claim` | `--last-name-claim` | `family_name` | JWT claim containing the user's last name. | `family_name` | `family_name` |

#### Validation Rules

| Field | CLI Flag | Default | Description | Entra ID Example | Cognito Example |
|-------|----------|---------|-------------|------------------|-----------------|
| `user_id_pattern` | `--user-id-pattern` | `None` | Regex pattern to validate user IDs. Rejects tokens with non-matching `user_id_claim` values. | `None` | `None` |
| `required_scopes` | — | `None` | List of scopes that must be present in the token. | `None` | `None` |
| `allowed_audiences` | `--allowed-audiences` | `None` | Allowed JWT `aud` claim values. If set, tokens with a different audience are rejected. | `["a1b2c3d4-e5f6-..."]` | `["1a2b3c4d5e6f..."]` |

#### Login Page Appearance

| Field | CLI Flag | Default | Description | Entra ID Example | Cognito Example |
|-------|----------|---------|-------------|------------------|-----------------|
| `logo_url` | `--logo-url` | `None` | URL to the provider's logo image shown on the login button. | `https://example.com/microsoft-logo.svg` | `https://example.com/aws-logo.svg` |
| `button_color` | `--button-color` | `None` | Hex color code for the login button. Must be in `#RRGGBB` format. | `#0078D4` | `#FF9900` |

#### Metadata (auto-populated)

| Field | Description |
|-------|-------------|
| `created_at` | ISO 8601 timestamp of when the provider was created. |
| `updated_at` | ISO 8601 timestamp of the last update. |
| `created_by` | Identifier of who created the provider. Set to `seed-script` when using the seed script, or the admin user's ID when created via the API. |

---

### Adding Providers After Initial Setup

Once the first provider is bootstrapped and you can log in as an admin, additional providers are managed entirely through the Admin UI:

**Admin Dashboard > Auth Providers > Add Provider**

The form supports OIDC Discovery (enter an issuer URL and click "Discover" to auto-fill endpoints), configurable JWT claim mappings, validation rules, and login page appearance customization.

### Migration from Hardcoded Entra ID

If the platform was previously using the hardcoded Entra ID configuration (via `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, etc.), both systems work simultaneously during migration:

1. Deploy with the new auth-providers table alongside the existing Entra ID env vars
2. Log in using the existing Entra ID flow (still works unchanged)
3. Create an Entra ID entry in Auth Providers through the Admin UI
4. Verify the new provider works
5. Remove the legacy `ENTRA_*` env vars in a later deployment

The JWT validation layer tries the new multi-provider system first, then falls back to the legacy Entra ID validator, so there is no disruption during the transition.

## Module Imports

All modules are properly packaged and can be imported directly:

```python
# Import shared auth utilities
from apis.shared.auth import get_current_user, User, StateStore

# Import agentcore modules
from agentcore.agent.agent import ChatbotAgent
from agentcore.local_tools.weather import get_weather
```

## Dependencies Overview

### Core Dependencies (all APIs)
- FastAPI 0.116.1
- Uvicorn 0.35.0
- Boto3 (AWS SDK)
- python-dotenv
- httpx
- PyJWT (authentication)

### AgentCore Dependencies (inference_api only)
- strands-agents 1.14.0
- strands-agents-tools 0.2.3
- bedrock-agentcore
- aws-opentelemetry-distro


### Development Dependencies (optional)
- pytest
- pytest-asyncio
- black (code formatter)
- ruff (linter)
- mypy (type checker)

## Migration Notes

This structure replaces the previous approach that used:
- Multiple `pyproject.toml` files (one per package)
- Multiple `setup.py` files
- Multiple `requirements.txt` files with `-e` editable installs
- Manual `sys.path` manipulation

**Benefits:**
- Single source of truth for dependencies
- Consistent import patterns across all code
- Proper package resolution
- Better IDE support
- Easier dependency updates
- Works in containers without path hacks
