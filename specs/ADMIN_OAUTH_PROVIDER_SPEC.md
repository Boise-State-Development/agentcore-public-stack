OAuth Provider Management Implementation Plan
Overview
Implement OAuth connection management per specs/ADMIN_OAUTH_PROVIDER_SPEC.md. Enables admins to configure OAuth providers (Google, Microsoft, Canvas, etc.) and users to connect accounts for MCP tool requests.

Phase 1: Infrastructure (CDK)
File to modify: infrastructure/lib/app-api-stack.ts
1.1 Add KMS Key (~line 920, after existing tables)
typescriptconst oauthTokenEncryptionKey = new kms.Key(this, "OAuthTokenEncryptionKey", {
  alias: getResourceName(config, "oauth-token-key"),
  description: "KMS key for encrypting OAuth user tokens at rest",
  enableKeyRotation: true,
  removalPolicy: config.environment === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});
1.2 OAuth Providers Table
typescriptconst oauthProvidersTable = new dynamodb.Table(this, "OAuthProvidersTable", {
  tableName: getResourceName(config, "oauth-providers"),
  partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  removalPolicy: config.environment === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
  encryption: dynamodb.TableEncryption.AWS_MANAGED,
});

oauthProvidersTable.addGlobalSecondaryIndex({
  indexName: "EnabledProvidersIndex",
  partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
1.3 OAuth User Tokens Table (with KMS encryption)
typescriptconst oauthUserTokensTable = new dynamodb.Table(this, "OAuthUserTokensTable", {
  tableName: getResourceName(config, "oauth-user-tokens"),
  partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  removalPolicy: config.environment === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
  encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
  encryptionKey: oauthTokenEncryptionKey,
});

oauthUserTokensTable.addGlobalSecondaryIndex({
  indexName: "ProviderUsersIndex",
  partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
1.4 Secrets Manager for Client Secrets
typescriptconst oauthClientSecretsSecret = new secretsmanager.Secret(this, "OAuthClientSecretsSecret", {
  secretName: getResourceName(config, "oauth-client-secrets"),
  description: "OAuth provider client secrets (JSON: {provider_id: secret})",
  removalPolicy: cdk.RemovalPolicy.RETAIN,
});
1.5 SSM Parameters
typescriptnew ssm.StringParameter(this, "OAuthProvidersTableNameParameter", {
  parameterName: `/${config.projectPrefix}/oauth/providers-table-name`,
  stringValue: oauthProvidersTable.tableName,
  tier: ssm.ParameterTier.STANDARD,
});
new ssm.StringParameter(this, "OAuthUserTokensTableNameParameter", {
  parameterName: `/${config.projectPrefix}/oauth/user-tokens-table-name`,
  stringValue: oauthUserTokensTable.tableName,
  tier: ssm.ParameterTier.STANDARD,
});
new ssm.StringParameter(this, "OAuthTokenEncryptionKeyArnParameter", {
  parameterName: `/${config.projectPrefix}/oauth/token-encryption-key-arn`,
  stringValue: oauthTokenEncryptionKey.keyArn,
  tier: ssm.ParameterTier.STANDARD,
});
1.6 IAM Grants & Environment Variables
Add to ECS task role grants:
typescriptoauthProvidersTable.grantReadWriteData(taskDefinition.taskRole);
oauthUserTokensTable.grantReadWriteData(taskDefinition.taskRole);
oauthTokenEncryptionKey.grantEncryptDecrypt(taskDefinition.taskRole);
oauthClientSecretsSecret.grantRead(taskDefinition.taskRole);
Add to container environment:
typescriptDYNAMODB_OAUTH_PROVIDERS_TABLE_NAME: oauthProvidersTable.tableName,
DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME: oauthUserTokensTable.tableName,
OAUTH_TOKEN_ENCRYPTION_KEY_ARN: oauthTokenEncryptionKey.keyArn,
OAUTH_CLIENT_SECRETS_ARN: oauthClientSecretsSecret.secretArn,

Phase 2: Backend Python Module
2.1 Dependencies
File: backend/pyproject.toml - Add:
tomlauthlib = "^1.3.0"
cachetools = "^5.3.0"
2.2 New Files to Create
FilePurposebackend/src/apis/app_api/oauth/__init__.pyModule exportsbackend/src/apis/app_api/oauth/models.pyPydantic modelsbackend/src/apis/app_api/oauth/encryption.pyKMS encrypt/decryptbackend/src/apis/app_api/oauth/token_cache.pyTTLCache (5 min)backend/src/apis/app_api/oauth/provider_repository.pyProvider CRUDbackend/src/apis/app_api/oauth/token_repository.pyToken CRUDbackend/src/apis/app_api/oauth/service.pyOAuth flow logicbackend/src/apis/app_api/oauth/routes.pyUser endpointsbackend/src/apis/app_api/admin/oauth/__init__.pyAdmin modulebackend/src/apis/app_api/admin/oauth/routes.pyAdmin endpoints
2.3 Models (oauth/models.py)

OAuthProviderType enum: google, microsoft, github, canvas, custom
OAuthConnectionStatus enum: connected, expired, revoked, needs_reauth
OAuthProvider dataclass with to_dynamo_item()/from_dynamo_item()
OAuthUserToken dataclass with encryption helpers
compute_scopes_hash() for change detection
Request/Response Pydantic models

2.4 Encryption (oauth/encryption.py)
pythonclass TokenEncryptionService:
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...
2.5 Service (oauth/service.py)
Key methods:

initiate_connect(provider_id, user_id) → authorization_url
handle_callback(code, state) → store encrypted tokens
get_decrypted_token(user_id, provider_id) → access_token
disconnect(user_id, provider_id) → delete tokens
check_needs_reauth(user_token, provider) → scope hash comparison

Reuse existing StateStore from apis/shared/auth/state_store.py for OAuth state.
2.6 Admin Routes (admin/oauth/routes.py)

POST /admin/oauth-providers/ - Create provider
GET /admin/oauth-providers/ - List all
GET /admin/oauth-providers/{id} - Get one
PATCH /admin/oauth-providers/{id} - Update
DELETE /admin/oauth-providers/{id} - Delete

2.7 User Routes (oauth/routes.py)

GET /oauth/providers - List available (filtered by user roles)
GET /oauth/connections - List user's connections
GET /oauth/connect/{provider_id} - Start OAuth flow
GET /oauth/callback - Handle callback, redirect to frontend
DELETE /oauth/connections/{provider_id} - Disconnect

2.8 Wire Routes
File: backend/src/apis/app_api/admin/routes.py - Add at bottom:
pythonfrom .oauth.routes import router as oauth_admin_router
router.include_router(oauth_admin_router)
File: backend/src/apis/app_api/main.py - Add:
pythonfrom apis.app_api.oauth.routes import router as oauth_router
app.include_router(oauth_router)

Phase 3: Admin UI (Angular)
3.1 New Files
FilePurposeadmin/oauth-providers/models/oauth-provider.model.tsTypeScript interfacesadmin/oauth-providers/services/oauth-providers.service.tsHTTP + resourceadmin/oauth-providers/pages/provider-list.page.tsList with search/filteradmin/oauth-providers/pages/provider-form.page.tsCreate/edit form
3.2 Models (oauth-provider.model.ts)
typescriptexport interface OAuthProvider {
  providerId: string;
  displayName: string;
  providerType: 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';
  authorizationEndpoint: string;
  tokenEndpoint: string;
  clientId: string;
  scopes: string[];
  allowedRoles: string[];
  enabled: boolean;
  iconName: string;
  createdAt: string;
  updatedAt: string;
}

export interface OAuthProviderCreateRequest { ... }
export interface OAuthProviderUpdateRequest { ... }
3.3 Service Pattern (follow app-roles.service.ts)
typescript@Injectable({ providedIn: 'root' })
export class OAuthProvidersService {
  readonly providersResource = resource({
    loader: async () => { ... }
  });
  // CRUD methods
}
3.4 List Page Pattern (follow role-list.page.ts)

Card grid layout with provider icons
Search by name signal
Filter by enabled status
Edit/Delete actions with tooltips

3.5 Form Page Pattern (follow role-form.page.ts)

Provider type dropdown with endpoint presets
Client ID / Client Secret (password field)
Scopes input (comma-separated or tags)
Role restrictions multi-select
Enabled toggle

3.6 Update Routes
File: frontend/ai.client/src/app/app.routes.ts - Add:
typescript{
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
3.7 Update Admin Dashboard
File: frontend/ai.client/src/app/admin/admin.page.ts - Add to features array:
typescript{
  title: 'OAuth Providers',
  description: 'Configure third-party OAuth integrations for tool access',
  icon: 'heroLink',
  route: '/admin/oauth-providers',
}

Phase 4: User Connections UI (Angular)
4.1 New Files
FilePurposesettings/connections/models/oauth-connection.model.tsInterfacessettings/connections/services/connections.service.tsHTTP + resourcesettings/connections/connections.page.tsMain page
4.2 Connections Page Features

List available providers with icons
Connect/Disconnect buttons
Status badges (Connected, Needs Reauth, Not Connected)
Handle callback query params (?success=true, ?error=...)
Toast notifications

4.3 Connect Flow
typescriptasync connect(providerId: string): Promise<void> {
  const response = await firstValueFrom(
    this.http.get<{ authorization_url: string }>(`${this.baseUrl}/oauth/connect/${providerId}`)
  );
  window.location.href = response.authorization_url;
}
4.4 Update Routes
File: frontend/ai.client/src/app/app.routes.ts - Add:
typescript{
  path: 'settings/connections',
  loadComponent: () => import('./settings/connections/connections.page').then(m => m.ConnectionsPage),
  canActivate: [authGuard],
},
4.5 Update User Dropdown
File: frontend/ai.client/src/app/components/topnav/components/user-dropdown.component.ts
Add after "My Files" menu item:
html<a cdkMenuItem routerLink="/settings/connections"
   class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-gray-700">
  <ng-icon name="heroLink" class="size-5 text-gray-400 dark:text-gray-500" />
  <span>Connections</span>
</a>

DynamoDB Schema Summary
OAuth Providers Table
Access PatternKeyIndexGet providerPK=PROVIDER#{id}, SK=CONFIGBaseList enabledGSI1PK=ENABLED#trueEnabledProvidersIndex
OAuth User Tokens Table
Access PatternKeyIndexGet user tokenPK=USER#{user_id}, SK=PROVIDER#{provider_id}BaseList user tokensPK=USER#{user_id}BaseList by providerGSI1PK=PROVIDER#{provider_id}ProviderUsersIndex

Security Considerations

Client secrets stored in Secrets Manager (never exposed to frontend)
User tokens encrypted with KMS at rest
State tokens are one-time use (reuse existing StateStore pattern)
PKCE required for all OAuth flows (S256)
Scope hash detects provider config changes, prompts re-auth
Role-based filtering on available providers


Verification Steps
Phase 1
bashcd infrastructure && npx cdk diff AppApiStack
npx cdk deploy AppApiStack
# Verify in AWS Console: DynamoDB tables, KMS key, Secrets Manager
Phase 2
bashcd backend
uv sync --extra agentcore --extra dev
uv run python -m pytest tests/test_oauth.py -v
# Start API and test endpoints with curl
Phase 3
bashcd frontend/ai.client
npm install && npm run build
# Navigate to /admin/oauth-providers, create test provider
Phase 4
bash# Navigate to /settings/connections
# Test Connect flow with configured provider
# Verify callback redirect and status display

Implementation Order

Phase 1: CDK - Deploy infrastructure first
Phase 2: Backend - Models → Repositories → Service → Routes
Phase 3: Admin UI - Models → Service → List → Form
Phase 4: User UI - Models → Service → Page → Dropdown menu item