# Requirements Document

## Introduction

This feature replaces the current multi-step, error-prone authentication bootstrap process with a WordPress-style first-boot experience powered by Amazon Cognito. Today, deploying the application requires manually configuring GitHub variables/secrets with OIDC provider details (issuer URL, client ID, client secret, etc.), deploying all stacks, then running a separate bootstrap seed workflow. If any value is mistyped, authentication breaks silently.

The new approach deploys a Cognito User Pool as the central identity broker during infrastructure provisioning. The first person to access the deployment signs up with username/password and becomes the system admin. That admin can then configure additional federated identity providers (Entra ID, Okta, Google, etc.) through the admin UI, which wires them into Cognito as federated identity providers. Because Cognito issues its own JWTs regardless of which upstream provider authenticated the user, the entire system can use a single AgentCore Runtime with a single Cognito JWT authorizer — eliminating the multi-runtime architecture, the Runtime Provisioner Lambda, and the bootstrap seed workflow for auth.

## Glossary

- **Cognito_User_Pool**: The Amazon Cognito User Pool deployed as part of the infrastructure stack, serving as the central identity broker for all authentication
- **Cognito_App_Client**: The Cognito User Pool App Client configured for the frontend application, enabling OAuth 2.0 / OIDC flows
- **Cognito_Domain**: The Cognito hosted UI domain (custom or prefix-based) used for OAuth 2.0 endpoints
- **First_Boot_Flow**: The initial setup experience where the first user to access a fresh deployment creates an admin account via Cognito username/password signup
- **Admin_User**: The first user who completes the First_Boot_Flow, automatically assigned the system admin role
- **Federated_Identity_Provider**: An external OIDC identity provider (Entra ID, Okta, Google, etc.) configured as a Cognito User Pool Identity Provider
- **App_API**: The FastAPI backend service running on Fargate that handles application logic, auth flows, and admin operations
- **Inference_Runtime**: The single AWS Bedrock AgentCore Runtime configured with a Cognito JWT authorizer
- **Auth_Providers_Table**: The DynamoDB table storing authentication provider configurations (PK: `AUTH_PROVIDER#{id}`)
- **System_Settings_Table**: The DynamoDB table (or item within an existing table) storing system-level configuration such as first-boot completion status
- **Frontend**: The Angular application served via CloudFront
- **Admin_UI**: The admin section of the Frontend used for managing authentication providers and system configuration
- **Bootstrap_Seed_Workflow**: The existing GitHub Actions workflow (`bootstrap-data-seeding.yml`) that seeds auth provider config from GitHub variables/secrets (being replaced for auth seeding)
- **Runtime_Provisioner_Lambda**: The existing Lambda function that provisions per-provider AgentCore Runtimes via DynamoDB Streams (being removed)
- **Runtime_Updater_Lambda**: The existing Lambda function that updates all provider runtimes when container images change (being removed)

## Requirements

### Requirement 1: Cognito User Pool Infrastructure

**User Story:** As a platform operator, I want a Cognito User Pool deployed automatically with the infrastructure stack, so that authentication is available immediately after deployment without manual configuration.

#### Acceptance Criteria

1. THE Infrastructure_Stack SHALL create a Cognito_User_Pool with username/password sign-in enabled and email as a required attribute
2. THE Infrastructure_Stack SHALL create a Cognito_App_Client configured with the authorization code grant flow, PKCE support, and scopes `openid`, `profile`, and `email`
3. THE Infrastructure_Stack SHALL create a Cognito_Domain (prefix-based using the project prefix, or custom domain when `domainName` is configured)
4. THE Infrastructure_Stack SHALL export the Cognito_User_Pool ID, Cognito_App_Client ID, Cognito_Domain URL, and Cognito issuer URL to SSM Parameter Store under `/${projectPrefix}/auth/cognito/...` paths
5. THE Infrastructure_Stack SHALL configure the Cognito_User_Pool with a password policy requiring a minimum of 8 characters, at least one uppercase letter, one lowercase letter, one number, and one special character
6. THE Infrastructure_Stack SHALL configure the Cognito_User_Pool with self-signup enabled for the first-boot flow, with admin-only signup enforced after first-boot completion
7. IF the `domainName` configuration is provided, THEN THE Infrastructure_Stack SHALL configure the Cognito_App_Client callback and logout URLs using that domain
8. IF the `domainName` configuration is not provided, THEN THE Infrastructure_Stack SHALL configure the Cognito_App_Client callback and logout URLs using the CloudFront distribution URL

### Requirement 2: First-Boot Admin Registration

**User Story:** As the first person to access a fresh deployment, I want to sign up with a username and password and become the system admin, so that I can configure the platform without needing pre-configured GitHub secrets.

#### Acceptance Criteria

1. WHEN a user accesses the Frontend for the first time on a fresh deployment, THE Frontend SHALL detect that first-boot has not been completed and display a first-boot setup page
2. THE First_Boot_Flow setup page SHALL collect a username, email address, and password from the user
3. WHEN the user submits the first-boot registration form, THE App_API SHALL create the user in the Cognito_User_Pool using the Cognito admin API
4. WHEN the user is successfully created in the Cognito_User_Pool, THE App_API SHALL assign the `system_admin` role to that user in the application's RBAC system
5. WHEN the admin user is created and assigned the admin role, THE App_API SHALL mark first-boot as completed in the System_Settings_Table
6. AFTER first-boot is marked as completed, THE App_API SHALL disable self-signup on the Cognito_User_Pool so that only federated identity providers or admin-created users can register
7. IF a second user attempts to access the first-boot registration endpoint after first-boot is completed, THEN THE App_API SHALL reject the request with a 409 Conflict status
8. THE First_Boot_Flow SHALL authenticate the newly created admin user and redirect to the admin dashboard upon successful registration

### Requirement 3: Cognito-Based Authentication Flow

**User Story:** As a user, I want to authenticate through Cognito using either username/password or my organization's identity provider, so that I have a consistent login experience regardless of authentication method.

#### Acceptance Criteria

1. THE App_API SHALL implement OAuth 2.0 authorization code flow with PKCE using Cognito as the authorization server
2. WHEN a user initiates login, THE Frontend SHALL redirect to the Cognito hosted UI or directly to the configured identity provider's authorization endpoint via Cognito
3. WHEN Cognito returns an authorization code, THE App_API SHALL exchange the code for Cognito-issued JWT tokens (ID token, access token, refresh token)
4. THE App_API SHALL validate all incoming JWT tokens against the Cognito_User_Pool's JWKS endpoint
5. WHEN a user authenticates via a Federated_Identity_Provider, THE Cognito_User_Pool SHALL issue its own JWT tokens containing the federated user's mapped attributes
6. THE App_API SHALL extract user identity (user ID, email, name, roles) from Cognito JWT token claims using configurable claim mappings
7. THE Frontend SHALL store Cognito tokens and include the access token in all authenticated API requests
8. WHEN a Cognito access token expires, THE Frontend SHALL use the refresh token to obtain new tokens from the Cognito token endpoint

### Requirement 4: Federated Identity Provider Management

**User Story:** As a system admin, I want to add, update, and remove external identity providers (Entra ID, Okta, Google) through the admin UI, so that users from different organizations can authenticate without infrastructure redeployment.

#### Acceptance Criteria

1. WHEN an admin creates a new auth provider via the Admin_UI, THE App_API SHALL register the provider as a Federated_Identity_Provider in the Cognito_User_Pool using the Cognito `CreateIdentityProvider` API
2. WHEN an admin creates a new auth provider, THE App_API SHALL store the provider configuration in the Auth_Providers_Table and store the client secret in Secrets Manager
3. WHEN an admin creates a new OIDC-type Federated_Identity_Provider, THE App_API SHALL configure the Cognito identity provider with the issuer URL, client ID, client secret, and attribute mappings
4. WHEN a Federated_Identity_Provider is created, THE App_API SHALL update the Cognito_App_Client to include the new provider in its list of supported identity providers
5. WHEN an admin updates a Federated_Identity_Provider's configuration, THE App_API SHALL update the corresponding Cognito identity provider using the `UpdateIdentityProvider` API
6. WHEN an admin deletes a Federated_Identity_Provider, THE App_API SHALL remove the provider from the Cognito_User_Pool using the `DeleteIdentityProvider` API and remove it from the Cognito_App_Client's supported providers list
7. IF the Cognito API call to create, update, or delete a Federated_Identity_Provider fails, THEN THE App_API SHALL return the error details to the Admin_UI and log the failure
8. THE App_API SHALL support OIDC-type federated providers with configurable attribute mappings for email, name, and sub claims
9. WHEN the `--discover` flag is enabled during provider creation, THE App_API SHALL fetch OIDC endpoints from the issuer URL's `.well-known/openid-configuration` endpoint to auto-populate the Cognito identity provider configuration

### Requirement 5: Single AgentCore Runtime with Cognito JWT Authorizer

**User Story:** As a platform operator, I want a single AgentCore Runtime that authenticates all users via Cognito JWTs, so that I do not need per-provider runtimes and the associated operational complexity.

#### Acceptance Criteria

1. THE Inference_API_Stack SHALL create exactly one AgentCore Runtime configured with a JWT authorizer pointing to the Cognito_User_Pool's OIDC discovery URL
2. THE Inference_Runtime JWT authorizer SHALL accept tokens issued by the Cognito_User_Pool regardless of which upstream identity provider the user authenticated with
3. THE Inference_Runtime JWT authorizer SHALL validate the `client_id` claim against the Cognito_App_Client ID (using `allowedClients`, not `allowedAudience`, because Cognito access tokens place the App Client ID in the `client_id` claim rather than the `aud` claim)
4. THE Frontend SHALL send the Cognito-issued access token when invoking the Inference_Runtime, using the single runtime endpoint for all users
5. THE Inference_API_Stack SHALL read the Cognito_User_Pool ID and Cognito_App_Client ID from SSM Parameter Store (exported by the Infrastructure_Stack)
6. THE Inference_API_Stack SHALL construct the Cognito OIDC discovery URL as `https://cognito-idp.{region}.amazonaws.com/{userPoolId}/.well-known/openid-configuration`

### Requirement 6: Remove Multi-Runtime Architecture

**User Story:** As a platform operator, I want the per-provider runtime provisioning system removed, so that the infrastructure is simpler and cheaper to operate.

#### Acceptance Criteria

1. THE App_API_Stack SHALL remove the Runtime_Provisioner_Lambda and its DynamoDB Stream trigger from the Auth_Providers_Table
2. THE App_API_Stack SHALL remove the Runtime_Updater_Lambda and its EventBridge trigger
3. THE App_API_Stack SHALL remove the SNS topic and CloudWatch alarms associated with runtime provisioning
4. THE Auth_Providers_Table schema SHALL retain the `agentcore_runtime_*` fields as deprecated but THE App_API SHALL stop writing to those fields for new providers
5. THE Frontend SHALL stop fetching per-provider runtime endpoint URLs and instead use the single Cognito-authorized runtime endpoint
6. THE App_API SHALL remove the `GET /auth/runtime-endpoint` endpoint that resolved per-provider runtime URLs

### Requirement 7: Remove Hardcoded Entra ID Configuration

**User Story:** As a platform operator, I want all hardcoded Entra ID configuration removed from the codebase, so that authentication is fully dynamic and provider-agnostic.

#### Acceptance Criteria

1. THE CDK configuration (`config.ts`) SHALL remove the `entraClientId`, `entraTenantId`, and `entraRedirectUri` properties from all interfaces and the `loadConfig` function
2. THE App_API_Stack SHALL remove Entra ID environment variables (`ENTRA_CLIENT_ID`, `ENTRA_TENANT_ID`, `ENTRA_REDIRECT_URI`) and secrets (`ENTRA_CLIENT_SECRET`) from the ECS task definition
3. THE GitHub Actions workflow files SHALL remove all Entra ID-specific variables (`CDK_ENTRA_CLIENT_ID`, `CDK_ENTRA_TENANT_ID`, `CDK_APP_API_ENTRA_REDIRECT_URI`) and secrets (`CDK_ENTRA_CLIENT_SECRET`)
4. THE `scripts/common/load-env.sh` SHALL remove Entra ID environment variable exports and context parameter generation
5. THE stack deployment scripts (`synth.sh`, `deploy.sh`) SHALL remove Entra ID context parameters
6. THE Backend test files SHALL replace Entra ID-specific test fixtures with generic OIDC provider fixtures

### Requirement 8: Remove Auth Bootstrap Seed Workflow for Auth Providers

**User Story:** As a platform operator, I want the GitHub Actions auth provider seeding workflow eliminated for authentication, so that I do not need to configure GitHub secrets for auth providers before deployment.

#### Acceptance Criteria

1. THE Bootstrap_Seed_Workflow SHALL remove the auth provider seeding job and all associated GitHub variables (`SEED_AUTH_PROVIDER_ID`, `SEED_AUTH_DISPLAY_NAME`, `SEED_AUTH_ISSUER_URL`, `SEED_AUTH_CLIENT_ID`, `SEED_AUTH_BUTTON_COLOR`) and secrets (`SEED_AUTH_CLIENT_SECRET`)
2. THE Bootstrap_Seed_Workflow SHALL retain seeding for non-auth data (quota tiers, quota assignments, Bedrock models) as those remain valid
3. THE `scripts/stack-bootstrap/seed.sh` and the Python seed script SHALL remove the auth provider seeding logic
4. THE deployment documentation SHALL be updated to describe the first-boot flow instead of GitHub variable configuration for authentication

### Requirement 9: Cognito Attribute Mapping for Federated Users

**User Story:** As a system admin, I want federated user attributes (email, name, groups) mapped correctly into Cognito, so that the application can identify and authorize federated users consistently.

#### Acceptance Criteria

1. WHEN configuring a Federated_Identity_Provider, THE App_API SHALL set up Cognito attribute mappings from the provider's claims to Cognito standard attributes (email, name, given_name, family_name, picture)
2. THE App_API SHALL map the federated provider's `sub` claim to a Cognito custom attribute to preserve the original provider user ID
3. WHEN a federated user signs in for the first time, THE Cognito_User_Pool SHALL create a linked user profile with the mapped attributes
4. THE App_API SHALL support configurable claim mappings per provider, allowing admins to specify which provider claims map to which Cognito attributes
5. IF a federated provider does not supply a required attribute (email), THEN THE Cognito_User_Pool SHALL reject the sign-in and THE App_API SHALL return a descriptive error to the user

### Requirement 10: Backend JWT Validation Migration

**User Story:** As a developer, I want the backend JWT validation simplified to validate only Cognito-issued tokens, so that the authentication code is easier to maintain and reason about.

#### Acceptance Criteria

1. THE shared auth module (`apis/shared/auth`) SHALL validate JWT tokens exclusively against the Cognito_User_Pool's JWKS endpoint
2. THE JWT validator SHALL verify the token issuer matches the Cognito_User_Pool issuer URL (`https://cognito-idp.{region}.amazonaws.com/{userPoolId}`)
3. THE JWT validator SHALL verify the token's `client_id` claim (for access tokens) or `aud` claim (for ID tokens) matches the Cognito_App_Client ID
4. THE JWT validator SHALL extract user identity from Cognito token claims: `sub` for user ID, `email` for email, `cognito:username` for username, and `custom:roles` or Cognito groups for roles
5. THE App_API SHALL remove the `GenericOIDCJWTValidator` class and its multi-provider issuer resolution logic, replacing it with a single-issuer Cognito validator
6. THE App_API SHALL remove the dependency on the Auth_Providers_Table for JWT validation (provider config is no longer needed at token validation time since Cognito is the sole issuer)

### Requirement 11: Frontend Authentication Flow Migration

**User Story:** As a developer, I want the frontend authentication flow updated to use Cognito, so that login works with both local username/password and federated providers through a single flow.

#### Acceptance Criteria

1. THE Frontend auth service SHALL use the Cognito OAuth 2.0 endpoints (authorize, token, logout) for all authentication flows
2. THE Frontend login page SHALL display a username/password form for Cognito native login alongside buttons for each configured Federated_Identity_Provider
3. WHEN a user clicks a federated provider button, THE Frontend SHALL redirect to the Cognito authorize endpoint with the `identity_provider` parameter set to the selected provider's Cognito name
4. THE Frontend SHALL fetch the list of available federated providers from the App_API's existing `GET /auth/providers` endpoint
5. THE Frontend SHALL use a single runtime endpoint URL (from environment configuration or SSM) for all Inference_Runtime invocations, removing the per-provider endpoint resolution
6. THE Frontend callback handler SHALL exchange the Cognito authorization code for tokens using the Cognito token endpoint

### Requirement 12: System Settings and First-Boot State

**User Story:** As a platform operator, I want the system to reliably track whether first-boot has been completed, so that the first-boot flow is only available once and subsequent deployments skip it.

#### Acceptance Criteria

1. THE App_API SHALL store a `SYSTEM_SETTINGS#first-boot` item in DynamoDB to track first-boot completion status
2. THE App_API SHALL expose a `GET /system/status` public endpoint that returns whether first-boot has been completed (without requiring authentication)
3. WHEN the Frontend loads, THE Frontend SHALL call the `GET /system/status` endpoint to determine whether to show the first-boot page or the login page
4. THE first-boot status check SHALL be idempotent and safe to call from multiple concurrent requests
5. IF the DynamoDB table does not contain the first-boot settings item, THEN THE App_API SHALL treat the system as not yet bootstrapped

### Requirement 13: Cognito User Pool Configuration for CDK

**User Story:** As a platform operator, I want Cognito configuration to follow the existing CDK context and SSM patterns, so that it integrates cleanly with the deployment pipeline.

#### Acceptance Criteria

1. THE CDK configuration (`config.ts`) SHALL add a `cognito` section to the `AppConfig` interface with properties for custom domain, callback URLs, and password policy overrides
2. THE Cognito_User_Pool configuration SHALL use CDK context values with environment variable overrides following the existing `CDK_` prefix convention
3. THE Infrastructure_Stack SHALL export Cognito resource identifiers to SSM using the `/${projectPrefix}/auth/cognito/` parameter path prefix
4. THE App_API_Stack SHALL import Cognito configuration from SSM parameters (not cross-stack references) following the existing SSM-based integration pattern
5. THE Inference_API_Stack SHALL import the Cognito User Pool ID and App Client ID from SSM to configure the runtime JWT authorizer
6. THE GitHub Actions workflows SHALL pass Cognito-related CDK context values following the existing variable/secret pattern (no Cognito secrets required since the User Pool is CDK-managed)
