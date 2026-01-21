OAuth Connection Management Implementation Plan
Overview
This feature enables admins to configure OAuth providers (Google Workspace, Canvas, etc.) and users to connect their accounts for use in MCP tool requests.

Key Decisions
Credentials Storage: Single Secrets Manager secret with all provider credentials (JSON object)
Callback Routing: Single /oauth/callback endpoint with state-based provider routing
Token Library: Authlib for OAuth flows with automatic refresh
Encryption: AWS KMS for token encryption at rest
Caching: In-memory TTL cache (5 min) to reduce KMS/DynamoDB calls
Scope Changes: Require re-authorization when admin modifies provider scopes
DynamoDB Schema
OAuth Providers Table ({prefix}-oauth-providers)
PK: PROVIDER#{provider_id}
SK: CONFIG

Attributes:
  - provider_id: str (e.g., "google-workspace")
  - display_name: str
  - provider_type: str (google, microsoft, canvas, generic)
  - authorization_endpoint: str
  - token_endpoint: str
  - scopes: List[str]
  - scopes_hash: str (SHA256 for change detection)
  - allowed_roles: List[str]
  - enabled: bool
  - icon_name: str (heroicons name)
  - created_at, updated_at, created_by

GSI: EnabledProvidersIndex
  GSI1PK: ENABLED#{enabled}
  GSI1SK: PROVIDER#{provider_id}
OAuth User Tokens Table ({prefix}-oauth-user-tokens)
PK: USER#{user_id}
SK: PROVIDER#{provider_id}

Attributes:
  - user_id: str
  - provider_id: str
  - encrypted_token: str (KMS encrypted JSON: access_token, refresh_token, expires_at)
  - token_expires_at: int (epoch)
  - scopes_granted: List[str]
  - scopes_hash: str (to detect if re-auth needed)
  - connected_at: int
  - last_refreshed_at: int
  - status: str (active | needs_reauth | revoked)

GSI: ProviderUsersIndex
  GSI1PK: PROVIDER#{provider_id}
  GSI1SK: USER#{user_id}
Phase 1: Infrastructure (CDK)
Files to Modify
infrastructure/lib/app-api-stack.ts

Add after existing table definitions (~line 920):

KMS Key for token encryption
typescript
const oauthTokensKey = new kms.Key(this, 'OAuthTokensKey', {
  description: 'KMS key for encrypting OAuth user tokens',
  enableKeyRotation: true,
  removalPolicy: cdk.RemovalPolicy.RETAIN,
});
OAuth Providers Table
typescript
const oauthProvidersTable = new dynamodb.Table(this, 'OAuthProvidersTable', {
  tableName: getResourceName(config, 'oauth-providers'),
  partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  encryption: dynamodb.TableEncryption.AWS_MANAGED,
  removalPolicy: config.environment === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});

oauthProvidersTable.addGlobalSecondaryIndex({
  indexName: 'EnabledProvidersIndex',
  partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
OAuth User Tokens Table
typescript
const oauthUserTokensTable = new dynamodb.Table(this, 'OAuthUserTokensTable', {
  tableName: getResourceName(config, 'oauth-user-tokens'),
  partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  encryption: dynamodb.TableEncryption.AWS_MANAGED,
  removalPolicy: config.environment === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});

oauthUserTokensTable.addGlobalSecondaryIndex({
  indexName: 'ProviderUsersIndex',
  partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
Secrets Manager Secret for OAuth credentials
typescript
const oauthCredentialsSecret = new secretsmanager.Secret(this, 'OAuthCredentialsSecret', {
  secretName: getResourceName(config, 'oauth-credentials'),
  description: 'OAuth provider credentials (client_id, client_secret per provider)',
  secretObjectValue: {
    providers: cdk.SecretValue.unsafePlainText('{}'),  // Empty JSON, populated by admin
  },
  removalPolicy: cdk.RemovalPolicy.RETAIN,
});
SSM Parameters
typescript
// Table names and ARNs
new ssm.StringParameter(this, 'OAuthProvidersTableNameParam', {
  parameterName: `/${config.projectPrefix}/oauth/providers-table-name`,
  stringValue: oauthProvidersTable.tableName,
});
new ssm.StringParameter(this, 'OAuthUserTokensTableNameParam', {
  parameterName: `/${config.projectPrefix}/oauth/user-tokens-table-name`,
  stringValue: oauthUserTokensTable.tableName,
});
new ssm.StringParameter(this, 'OAuthKmsKeyArnParam', {
  parameterName: `/${config.projectPrefix}/oauth/kms-key-arn`,
  stringValue: oauthTokensKey.keyArn,
});
new ssm.StringParameter(this, 'OAuthCredentialsSecretArnParam', {
  parameterName: `/${config.projectPrefix}/oauth/credentials-secret-arn`,
  stringValue: oauthCredentialsSecret.secretArn,
});
IAM Grants (add to ECS task role grants section)
typescript
oauthProvidersTable.grantReadWriteData(taskDefinition.taskRole);
oauthUserTokensTable.grantReadWriteData(taskDefinition.taskRole);
oauthTokensKey.grantEncryptDecrypt(taskDefinition.taskRole);
oauthCredentialsSecret.grantRead(taskDefinition.taskRole);
Environment Variables (add to container environment)
typescript
DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME: oauthProvidersTable.tableName,
DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME: oauthUserTokensTable.tableName,
OAUTH_KMS_KEY_ARN: oauthTokensKey.keyArn,
OAUTH_CREDENTIALS_SECRET_ARN: oauthCredentialsSecret.secretArn,
Verification
bash
cd infrastructure && npx cdk diff AppApiStack
npx cdk deploy AppApiStack
# Verify in AWS Console: DynamoDB tables, KMS key, Secrets Manager secret
Phase 2: Backend (Python/FastAPI)
Dependencies
backend/pyproject.toml - Add:

toml
authlib = "^1.3.0"
cachetools = "^5.3.0"
New Files
backend/src/apis/app_api/oauth/__init__.py

python
from .routes import router, admin_router
from .service import OAuthService, get_oauth_service

__all__ = ["router", "admin_router", "OAuthService", "get_oauth_service"]
backend/src/apis/app_api/oauth/models.py

OAuthProvider - Provider configuration model
OAuthUserToken - User token model
OAuthProviderCreate, OAuthProviderUpdate - Request models
OAuthProviderResponse, OAuthProviderListResponse - Response models
OAuthConnection, OAuthConnectionListResponse - User connection models
OAuthConnectResponse - Contains authorization_url
OAuthCredentials - Client ID/secret for a provider
backend/src/apis/app_api/oauth/encryption.py

TokenEncryption class with KMS encrypt/decrypt
Lazy boto3 client initialization
JSON serialization for token dict
backend/src/apis/app_api/oauth/token_cache.py

TokenCache class using cachetools.TTLCache
5-minute default TTL, 1000 max entries
Key format: {user_id}:{provider_id}
backend/src/apis/app_api/oauth/repository.py

OAuthProviderRepository - Provider CRUD
OAuthUserTokenRepository - Token CRUD
Factory functions: get_provider_repository(), get_token_repository()
backend/src/apis/app_api/oauth/service.py

OAuthService class with:
get_available_providers(user) - Filter by user roles
initiate_oauth_flow(user_id, provider_id) - Generate auth URL with state
handle_callback(code, state) - Exchange code, encrypt & store tokens
get_authenticated_client(user_id, provider_id) - Return Authlib client with auto-refresh
disconnect(user_id, provider_id) - Revoke and delete tokens
get_user_connections(user_id) - List connections with status
_check_reauth_needed(connection, provider) - Compare scope hashes
Factory: get_oauth_service()
backend/src/apis/app_api/oauth/routes.py

admin_router (prefix="/oauth", tags=["admin-oauth"])
POST /providers - Create provider
GET /providers - List all providers
GET /providers/{provider_id} - Get provider
PATCH /providers/{provider_id} - Update provider
DELETE /providers/{provider_id} - Delete provider
PUT /providers/{provider_id}/credentials - Update credentials
router (prefix="/oauth", tags=["oauth"])
GET /providers - List available to user
GET /connect/{provider_id} - Start OAuth flow
GET /callback - Handle callback
GET /connections - List user connections
DELETE /connections/{provider_id} - Disconnect
Files to Modify
backend/src/apis/app_api/admin/routes.py

python
# Add at end
from apis.app_api.oauth.routes import admin_router as oauth_admin_router
router.include_router(oauth_admin_router)
backend/src/apis/app_api/main.py (or wherever user routes are registered)

python
from apis.app_api.oauth.routes import router as oauth_router
app.include_router(oauth_router)
OAuth Flow Implementation
Initiate (GET /oauth/connect/{provider_id}):
Verify user has role in provider's allowed_roles
Load provider config and credentials from Secrets Manager
Generate state token with {user_id, provider_id, nonce} (reuse state_store)
Build authorization URL with scopes
Return { authorization_url }
Callback (GET /oauth/callback):
Extract code and state from query params
Validate state (atomic delete from state_store)
Exchange code for tokens using Authlib
Encrypt tokens with KMS
Store in DynamoDB with status=active
Redirect to frontend: /settings/connections?success=true&provider={id}
Get Client (get_authenticated_client):
Check cache first
If miss, fetch from DynamoDB, decrypt
Create AsyncOAuth2Client with update_token callback
Callback encrypts and saves refreshed tokens
Cache the decrypted token
Verification
bash
cd backend/src
python -m pytest tests/test_oauth.py -v
# Manual: Create provider via API, test connect flow
Phase 3: Admin UI (Angular)
New Files
frontend/ai.client/src/app/admin/oauth-providers/models/oauth-provider.model.ts

typescript
export interface OAuthProvider {
  providerId: string;
  displayName: string;
  providerType: 'google' | 'microsoft' | 'canvas' | 'generic';
  authorizationEndpoint: string;
  tokenEndpoint: string;
  scopes: string[];
  allowedRoles: string[];
  enabled: boolean;
  iconName: string;
  createdAt: string;
  updatedAt: string;
}

export interface OAuthProviderCreateRequest { ... }
export interface OAuthProviderUpdateRequest { ... }
export interface OAuthProviderListResponse { providers: OAuthProvider[]; total: number; }
export interface OAuthCredentialsRequest { clientId: string; clientSecret: string; }
frontend/ai.client/src/app/admin/oauth-providers/services/oauth-providers.service.ts

Inject HttpClient, AuthService
rolesResource = resource({ loader: ... }) for reactive list
CRUD methods returning promises
updateCredentials(providerId, credentials) method
frontend/ai.client/src/app/admin/oauth-providers/pages/provider-list.page.ts

Search and filter signals
Card-based list with enable/disable toggle
Edit and delete actions
"New Provider" button
frontend/ai.client/src/app/admin/oauth-providers/pages/provider-form.page.ts

Reactive form with validation
Provider type dropdown (affects endpoint defaults)
Scopes as comma-separated or tag input
Role multi-select (from AppRolesService)
Separate "Credentials" section for client_id/secret
Icon picker (heroicons subset)
Files to Modify
frontend/ai.client/src/app/app.routes.ts

typescript
{
  path: 'admin/oauth-providers',
  loadComponent: () => import('./admin/oauth-providers/pages/provider-list.page').then(m => m.ProviderListPage),
  canActivate: [adminGuard],
},
{
  path: 'admin/oauth-providers/new',
  loadComponent: () => import('./admin/oauth-providers/pages/provider-form.page').then(m => m.ProviderFormPage),
  canActivate: [adminGuard],
},
{
  path: 'admin/oauth-providers/edit/:providerId',
  loadComponent: () => import('./admin/oauth-providers/pages/provider-form.page').then(m => m.ProviderFormPage),
  canActivate: [adminGuard],
},
frontend/ai.client/src/app/admin/admin.page.ts Add OAuth Providers card to admin dashboard grid.

Verification
Navigate to /admin/oauth-providers
Create a test provider (e.g., Google)
Verify list, edit, enable/disable work
Verify credentials update (masked display)
Phase 4: User UI (Angular)
New Files
frontend/ai.client/src/app/settings/connections/models/oauth-connection.model.ts

typescript
export interface AvailableProvider {
  providerId: string;
  displayName: string;
  iconName: string;
  connected: boolean;
  status?: 'active' | 'needs_reauth' | 'revoked';
  connectedAt?: string;
}

export interface OAuthConnection {
  providerId: string;
  displayName: string;
  iconName: string;
  status: 'active' | 'needs_reauth' | 'revoked';
  scopesGranted: string[];
  connectedAt: string;
  lastRefreshedAt?: string;
}
frontend/ai.client/src/app/settings/connections/services/oauth-connections.service.ts

availableProvidersResource - Reactive resource for available providers
connectionsResource - Reactive resource for user's connections
connect(providerId) - Returns authorization URL, handles redirect
disconnect(providerId) - Revoke connection
frontend/ai.client/src/app/settings/connections/connections.page.ts

Display available providers with Connect/Disconnect buttons
Status badges (green=active, yellow=needs_reauth)
Handle callback query params (?success=true or ?error=...)
Show toast notifications for success/error
"Reconnect" button for needs_reauth status
Files to Modify
frontend/ai.client/src/app/app.routes.ts

typescript
{
  path: 'settings/connections',
  loadComponent: () => import('./settings/connections/connections.page').then(m => m.ConnectionsPage),
  canActivate: [authGuard],
},
frontend/ai.client/src/app/components/topnav/components/user-dropdown.component.ts Add menu item after "My Files":

html
<a cdkMenuItem routerLink="/settings/connections"
   class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-gray-700">
  <ng-icon name="heroLink" class="size-5 text-gray-400 dark:text-gray-500" />
  <span>Connections</span>
</a>
OAuth Connect Flow (Frontend)
User clicks "Connect" on a provider
Service calls GET /oauth/connect/{provider_id}
Response: { authorization_url: "https://accounts.google.com/..." }
Frontend does window.location.href = authorization_url
User authenticates with provider
Provider redirects to backend: /oauth/callback?code=xxx&state=yyy
Backend processes, redirects to: /settings/connections?success=true&provider=google-workspace
Frontend shows success toast, refreshes connections list
Verification
Navigate to /settings/connections
Verify available providers filtered by user roles
Test full Connect flow with a configured provider
Verify Disconnect removes connection
Verify needs_reauth status shows Reconnect button
Testing Strategy
Unit Tests
backend/tests/test_oauth_encryption.py - KMS encrypt/decrypt
backend/tests/test_oauth_repository.py - DynamoDB operations
backend/tests/test_oauth_service.py - Business logic
Integration Tests
Full OAuth flow with mock provider
Token refresh simulation
Scope change detection
Manual Testing
Deploy infrastructure (Phase 1)
Create provider via admin UI (Phase 3)
Connect as user (Phase 4)
Verify token storage and encryption
Disconnect and verify cleanup
Security Considerations
Client secrets never exposed to frontend - stored in Secrets Manager
User tokens encrypted with KMS at rest
State tokens are one-time use (atomic delete)
Role filtering ensures users only see allowed providers
PKCE used for OAuth flows (code_challenge_method='S256')
Token refresh happens server-side only
Files Summary
Phase 1 (Infrastructure)
infrastructure/lib/app-api-stack.ts - Add tables, KMS, secrets, SSM, IAM
Phase 2 (Backend)
backend/pyproject.toml - Add authlib, cachetools
backend/src/apis/app_api/oauth/__init__.py - Module exports
backend/src/apis/app_api/oauth/models.py - Pydantic models
backend/src/apis/app_api/oauth/encryption.py - KMS helper
backend/src/apis/app_api/oauth/token_cache.py - TTL cache
backend/src/apis/app_api/oauth/repository.py - DynamoDB repos
backend/src/apis/app_api/oauth/service.py - OAuth logic
backend/src/apis/app_api/oauth/routes.py - FastAPI routes
backend/src/apis/app_api/admin/routes.py - Include admin router
backend/src/apis/app_api/main.py - Include user router
Phase 3 (Admin UI)
frontend/ai.client/src/app/admin/oauth-providers/models/oauth-provider.model.ts
frontend/ai.client/src/app/admin/oauth-providers/services/oauth-providers.service.ts
frontend/ai.client/src/app/admin/oauth-providers/pages/provider-list.page.ts
frontend/ai.client/src/app/admin/oauth-providers/pages/provider-form.page.ts
frontend/ai.client/src/app/app.routes.ts - Add admin routes
frontend/ai.client/src/app/admin/admin.page.ts - Add dashboard card
Phase 4 (User UI)
frontend/ai.client/src/app/settings/connections/models/oauth-connection.model.ts
frontend/ai.client/src/app/settings/connections/services/oauth-connections.service.ts
frontend/ai.client/src/app/settings/connections/connections.page.ts
frontend/ai.client/src/app/app.routes.ts - Add user route
frontend/ai.client/src/app/components/topnav/components/user-dropdown.component.ts - Add menu item