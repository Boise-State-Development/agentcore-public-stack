# Implementation Plan: Cognito First-Boot Authentication

## Overview

Replace the multi-step auth bootstrap (GitHub secrets → seed workflow → multi-runtime provisioning) with a WordPress-style first-boot experience powered by Amazon Cognito. Implementation proceeds bottom-up: CDK infrastructure first, then backend APIs, then frontend migration, then removal of legacy components. Each task builds on the previous, with property-based tests validating correctness properties from the design.

## Tasks

- [x] 1. CDK Configuration and Cognito User Pool Infrastructure
  - [x] 1.1 Add CognitoConfig to CDK configuration
    - Add `CognitoConfig` interface to `infrastructure/lib/config.ts` with `domainPrefix`, `callbackUrls`, `logoutUrls`, `passwordMinLength` properties
    - Add `cognito: CognitoConfig` to `AppConfig` interface
    - Implement `loadConfig()` loading with `CDK_COGNITO_*` environment variable overrides and CDK context fallbacks
    - _Requirements: 13.1, 13.2_

  - [ ]* 1.2 Write property test for CDK config loading with env var overrides
    - **Property 17: CDK config loading with environment variable overrides**
    - Generate random env var and context values with Hypothesis, verify env var takes precedence over context value
    - **Validates: Requirements 13.2**

  - [x] 1.3 Create Cognito User Pool, App Client, and Domain in Infrastructure Stack
    - Add Cognito User Pool with `cognito.UserPool` L2 construct in `infrastructure/lib/infrastructure-stack.ts`
    - Configure password policy (min 8 chars, uppercase, lowercase, digit, symbol), self-signup enabled, email required
    - Create App Client with authorization code grant, PKCE, scopes `openid profile email`, no client secret (SPA)
    - Derive callback/logout URLs from `domainName` config (HTTPS) or fallback to `localhost:4200`
    - Create Cognito Domain with prefix-based domain using `projectPrefix`
    - Export User Pool ID, ARN, App Client ID, Domain URL, and Issuer URL to SSM under `/${projectPrefix}/auth/cognito/` paths
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 13.3_

  - [ ]* 1.4 Write property test for SSM export path correctness
    - **Property 1: Cognito SSM export path correctness**
    - Generate random valid project prefixes, verify SSM parameter paths begin with `/${projectPrefix}/auth/cognito/` and include all 5 required keys
    - **Validates: Requirements 1.4, 13.3**

  - [ ]* 1.5 Write property test for callback URL derivation
    - **Property 2: Callback URL derivation from domain configuration**
    - Generate random domain names (or None), verify callback URLs use `https://{domainName}/auth/callback` when domain provided, `http://localhost:4200/auth/callback` otherwise
    - **Validates: Requirements 1.7, 1.8**

- [x] 2. Checkpoint - Verify CDK infrastructure compiles and synthesizes
  - Ensure `npm run build` and `npx cdk synth` succeed in `infrastructure/`
  - Ensure all property tests pass, ask the user if questions arise.

- [x] 3. System Settings and First-Boot Backend
  - [x] 3.1 Create system settings repository and models
    - Create `backend/src/apis/app_api/system/` module with `models.py` defining `FirstBootRequest`, `FirstBootResponse`, `SystemStatusResponse` Pydantic models
    - Create `repository.py` with DynamoDB operations for `SYSTEM_SETTINGS#first-boot` item using `attribute_not_exists(PK)` conditional writes for race condition protection
    - _Requirements: 12.1, 12.4, 12.5_

  - [x] 3.2 Implement system status endpoint
    - Add `GET /system/status` public endpoint (no auth required) in `backend/src/apis/app_api/system/routes.py`
    - Return `first_boot_completed: true` if `SYSTEM_SETTINGS#first-boot` item exists with `completed=true`, else `false`
    - Return `first_boot_completed: false` as safe default on DynamoDB read failure
    - Register router in App API main.py
    - _Requirements: 12.2, 12.3, 12.5_

  - [ ]* 3.3 Write property test for system status round-trip
    - **Property 15: System status round-trip**
    - Generate random DynamoDB states (with/without first-boot item), verify endpoint response matches expected boolean
    - **Validates: Requirements 12.1, 12.2**

  - [x] 3.4 Implement first-boot endpoint
    - Add `POST /system/first-boot` public endpoint (no auth required) in `routes.py`
    - Atomic check: if first-boot already completed, return 409 Conflict
    - Create user in Cognito via `AdminCreateUser` + `AdminSetUserPassword` (permanent)
    - Create user record in Users DynamoDB table with `system_admin` role
    - Mark first-boot completed in DynamoDB with conditional write
    - Disable self-signup via `UpdateUserPool` setting `AllowAdminCreateUserOnly=true`
    - Return 400 for invalid password (Cognito policy violation), 409 for race conditions
    - _Requirements: 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [ ]* 3.5 Write property test for first-boot creates admin
    - **Property 3: First-boot creates admin with correct role**
    - Generate random valid usernames/emails/passwords, run first-boot against mocked Cognito+DynamoDB, verify admin user exists with `system_admin` role and first-boot item is `completed=true`
    - **Validates: Requirements 2.3, 2.4, 2.5**

  - [ ]* 3.6 Write property test for first-boot rejection after completion
    - **Property 4: First-boot rejection after completion**
    - Generate random valid first-boot requests, run first-boot twice, verify second returns 409 and system state unchanged
    - **Validates: Requirements 2.7**

  - [ ]* 3.7 Write property test for first-boot disables self-signup
    - **Property 5: First-boot disables self-signup**
    - Generate random first-boot requests, verify `UpdateUserPool` is called with `AllowAdminCreateUserOnly=true` after success
    - **Validates: Requirements 2.6**

  - [ ]* 3.8 Write property test for concurrent first-boot safety
    - **Property 16: Concurrent first-boot safety**
    - Generate random concurrent first-boot requests, verify exactly one succeeds (200) and all others fail (409), and exactly one admin user and one first-boot item exist
    - **Validates: Requirements 12.4**

- [x] 4. Checkpoint - Verify first-boot backend works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Backend JWT Validation Migration
  - [x] 5.1 Implement CognitoJWTValidator
    - Create `backend/src/apis/shared/auth/cognito_jwt_validator.py` with `CognitoJWTValidator` class
    - Validate JWT signature against Cognito JWKS endpoint, verify issuer matches `https://cognito-idp.{region}.amazonaws.com/{userPoolId}`, verify `client_id` claim (access tokens) or `aud` claim (ID tokens) matches App Client ID, verify expiration
    - Extract user identity: `sub` → `user_id`, `email` → `email`, `name` (fallback `cognito:username`) → `name`, `cognito:groups` → `roles`, `picture` → `picture`
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

  - [x] 5.2 Update get_current_user dependency to use CognitoJWTValidator
    - Update `backend/src/apis/shared/auth/dependencies.py` to instantiate `CognitoJWTValidator` using Cognito User Pool ID, App Client ID, and region from environment variables
    - Remove `GenericOIDCJWTValidator` instantiation and multi-provider issuer resolution logic
    - Remove dependency on Auth_Providers_Table for JWT validation
    - _Requirements: 10.5, 10.6_

  - [ ]* 5.3 Write property test for JWT validation
    - **Property 6: Cognito JWT validation rejects invalid tokens**
    - Generate random JWT payloads with valid/invalid issuers, audiences, and expiration, verify accept/reject behavior
    - **Validates: Requirements 3.4, 10.1, 10.2, 10.3**

  - [ ]* 5.4 Write property test for claim extraction
    - **Property 7: Cognito claim extraction correctness**
    - Generate random Cognito-style JWT payloads with `sub`, `email`, `name`, `cognito:groups`, verify User object fields match claims correctly
    - **Validates: Requirements 3.6, 10.4**

- [x] 6. Federated Identity Provider Management
  - [x] 6.1 Extend auth provider create to register in Cognito
    - Update the existing auth provider creation logic in `backend/src/apis/app_api/admin/` to call Cognito `CreateIdentityProvider` with OIDC provider details (issuer URL, client ID, client secret, attribute mappings)
    - Call `UpdateUserPoolClient` to add new provider to `SupportedIdentityProviders`
    - Implement rollback: if `UpdateUserPoolClient` fails, delete the identity provider from Cognito; if DynamoDB write fails, delete from Cognito
    - Store `cognitoProviderName` field in Auth_Providers_Table DynamoDB item
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.7, 9.1, 9.2_

  - [x] 6.2 Extend auth provider update to sync to Cognito
    - Update existing auth provider update logic to call Cognito `UpdateIdentityProvider` with changed OIDC configuration and attribute mappings
    - _Requirements: 4.5_

  - [x] 6.3 Extend auth provider delete to remove from Cognito
    - Update existing auth provider deletion logic to call `DeleteIdentityProvider` and remove provider from App Client's `SupportedIdentityProviders` via `UpdateUserPoolClient`
    - Delete from DynamoDB and Secrets Manager (existing logic)
    - Handle "not found" from Cognito gracefully (idempotent delete)
    - _Requirements: 4.6_

  - [x] 6.4 Add configurable attribute mappings and OIDC discovery
    - Support admin-specified custom attribute mappings per provider (email, name, given_name, family_name, picture, custom:provider_sub)
    - When `--discover` flag is enabled, fetch `.well-known/openid-configuration` from issuer URL to auto-populate Cognito identity provider configuration
    - _Requirements: 4.8, 4.9, 9.4_

  - [x] 6.5 Add Cognito IAM permissions to App API task role
    - Add IAM policy statement to App API ECS task role in `infrastructure/lib/app-api-stack.ts` granting `cognito-idp:CreateIdentityProvider`, `UpdateIdentityProvider`, `DeleteIdentityProvider`, `DescribeIdentityProvider`, `ListIdentityProviders`, `UpdateUserPoolClient`, `DescribeUserPoolClient`, `AdminCreateUser`, `AdminSetUserPassword`, `AdminGetUser`, `UpdateUserPool` on the Cognito User Pool ARN
    - Import Cognito User Pool ARN from SSM parameter
    - _Requirements: 4.1, 13.4_

  - [ ]* 6.6 Write property test for provider creation Cognito registration
    - **Property 8: Provider creation registers in Cognito with correct attribute mappings**
    - Generate random provider configs with custom attribute mappings, verify `CreateIdentityProvider` call includes correct `ProviderDetails` and `AttributeMapping`
    - **Validates: Requirements 4.1, 4.3, 9.1, 9.2, 9.4**

  - [ ]* 6.7 Write property test for provider creation DynamoDB + Secrets Manager
    - **Property 9: Provider creation stores configuration in DynamoDB and Secrets Manager**
    - Generate random provider configs, verify DynamoDB item with PK `AUTH_PROVIDER#{providerId}` and Secrets Manager write
    - **Validates: Requirements 4.2**

  - [ ]* 6.8 Write property test for provider creation App Client update
    - **Property 10: Provider creation updates App Client supported providers**
    - Generate random sequences of provider creations, verify `SupportedIdentityProviders` grows correctly and always includes `COGNITO`
    - **Validates: Requirements 4.4**

  - [ ]* 6.9 Write property test for provider update Cognito sync
    - **Property 11: Provider update syncs to Cognito**
    - Generate random provider updates, verify `UpdateIdentityProvider` is called with correct params
    - **Validates: Requirements 4.5**

  - [ ]* 6.10 Write property test for provider deletion cleanup
    - **Property 12: Provider deletion removes from Cognito and App Client**
    - Generate random provider creation+deletion sequences, verify all resources cleaned up from Cognito, DynamoDB, and Secrets Manager
    - **Validates: Requirements 4.6**

- [x] 7. Checkpoint - Verify federated provider management works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Single AgentCore Runtime with Cognito JWT Authorizer
  - [x] 8.1 Update Inference API Stack for single Cognito-authorized runtime
    - Modify `infrastructure/lib/inference-api-stack.ts` to create a single CDK-managed `bedrock.CfnRuntime` with `customJwtAuthorizer` pointing to Cognito OIDC discovery URL
    - Import Cognito User Pool ID and App Client ID from SSM parameters
    - Construct discovery URL as `https://cognito-idp.{region}.amazonaws.com/{userPoolId}/.well-known/openid-configuration`
    - Set `allowedClients` to the Cognito App Client ID (not `allowedAudience`, because Cognito access tokens use the `client_id` claim for the App Client ID)
    - _Requirements: 5.1, 5.2, 5.3, 5.5, 5.6, 13.5_

  - [ ]* 8.2 Write property test for Cognito discovery URL construction
    - **Property 13: Cognito discovery URL construction**
    - Generate random valid AWS region strings and User Pool IDs, verify URL format matches `https://cognito-idp.{region}.amazonaws.com/{userPoolId}/.well-known/openid-configuration`
    - **Validates: Requirements 5.6**

- [x] 9. Remove Multi-Runtime Architecture
  - [x] 9.1 Remove Runtime Provisioner Lambda and Runtime Updater Lambda
    - Delete `backend/lambda-functions/runtime-provisioner/` directory
    - Delete `backend/lambda-functions/runtime-updater/` directory
    - Remove Runtime Provisioner Lambda, DynamoDB Stream trigger, SNS topic, and CloudWatch alarms from `infrastructure/lib/app-api-stack.ts`
    - Remove Runtime Updater Lambda and EventBridge trigger from `infrastructure/lib/app-api-stack.ts`
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 9.2 Remove per-provider runtime endpoint resolution
    - Remove `GET /auth/runtime-endpoint` endpoint from App API backend
    - Stop writing `agentcore_runtime_*` fields for new providers in Auth_Providers_Table
    - _Requirements: 6.4, 6.6_

  - [ ]* 9.3 Write property test for no deprecated runtime fields on new providers
    - **Property 14: New providers do not write deprecated runtime fields**
    - Generate random new provider configs, verify DynamoDB items do not contain non-null `agentcoreRuntimeArn`, `agentcoreRuntimeId`, or `agentcoreRuntimeEndpointUrl`
    - **Validates: Requirements 6.4**

- [x] 10. Remove Hardcoded Entra ID Configuration
  - [x] 10.1 Remove Entra ID from CDK config and stacks
    - Remove `entraClientId`, `entraTenantId` from `AppConfig` interface and `entraRedirectUri` from `AppApiConfig` in `infrastructure/lib/config.ts`
    - Remove all Entra ID loading logic from `loadConfig()`
    - Remove `ENTRA_CLIENT_ID`, `ENTRA_TENANT_ID`, `ENTRA_REDIRECT_URI` env vars and `ENTRA_CLIENT_SECRET` secret from App API ECS task definition in `app-api-stack.ts`
    - _Requirements: 7.1, 7.2_

  - [x] 10.2 Remove Entra ID from scripts and GitHub workflows
    - Remove `CDK_ENTRA_CLIENT_ID`, `CDK_ENTRA_TENANT_ID`, `CDK_APP_API_ENTRA_REDIRECT_URI` variables and `CDK_ENTRA_CLIENT_SECRET` secret references from GitHub Actions workflow files
    - Remove Entra ID environment variable exports and context parameter generation from `scripts/common/load-env.sh`
    - Remove Entra ID context parameters from `scripts/stack-infrastructure/synth.sh`, `scripts/stack-infrastructure/deploy.sh`, and other stack deployment scripts
    - _Requirements: 7.3, 7.4, 7.5_

  - [x] 10.3 Remove GenericOIDCJWTValidator and multi-provider auth logic
    - Delete `GenericOIDCJWTValidator` class and its multi-provider issuer resolution logic from `backend/src/apis/shared/auth/`
    - Update any backend test files that use Entra ID-specific fixtures to use generic OIDC provider fixtures
    - _Requirements: 7.6, 10.5_

- [x] 11. Remove Auth Bootstrap Seed Workflow
  - [x] 11.1 Remove auth provider seeding from bootstrap workflow
    - Remove auth provider seeding job and all `SEED_AUTH_*` variables/secrets from `bootstrap-data-seeding.yml` GitHub Actions workflow
    - Remove auth provider seeding logic from `scripts/stack-bootstrap/seed.sh` and the Python seed script (`seed_bootstrap_data.py`)
    - Retain quota tier, quota assignment, and Bedrock model seeding
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 12. Checkpoint - Verify all removals compile cleanly
  - Ensure CDK `npm run build` succeeds after all removals
  - Ensure backend tests pass after removing GenericOIDCJWTValidator and lambda functions
  - Ensure all property tests pass, ask the user if questions arise.

- [x] 13. Frontend Authentication Flow Migration
  - [x] 13.1 Update Angular AuthService for Cognito OAuth 2.0
    - Update `frontend/ai.client/src/app/auth/auth.service.ts` to use Cognito OAuth 2.0 endpoints (authorize, token, logout)
    - Implement PKCE flow: generate code verifier, code challenge, state parameter
    - Add `login(providerId?: string)` method that redirects to Cognito authorize endpoint, with optional `identity_provider` parameter for federated providers
    - Add `handleCallback(code, state)` method that exchanges authorization code for Cognito tokens via the Cognito token endpoint
    - Implement token storage and refresh token flow
    - _Requirements: 3.1, 3.2, 3.3, 3.7, 3.8, 11.1, 11.6_

  - [x] 13.2 Update login page for Cognito native + federated login
    - Update login component to display username/password form for Cognito native login
    - Add federated provider buttons fetched from `GET /auth/providers` endpoint
    - Clicking a federated provider button calls `login(providerId)` to redirect to Cognito with `identity_provider` parameter
    - _Requirements: 11.2, 11.3, 11.4_

  - [x] 13.3 Update frontend to use single runtime endpoint
    - Remove per-provider runtime endpoint resolution and `getRuntimeEndpoint()` API call
    - Update all Inference Runtime invocations to use a single endpoint URL from environment configuration
    - Send Cognito-issued access token for all runtime invocations
    - _Requirements: 5.4, 6.5, 11.5_

  - [x] 13.4 Add first-boot page to frontend
    - Create first-boot setup component that collects username, email, and password
    - On app load, call `GET /system/status` to determine whether to show first-boot page or login page
    - On form submit, call `POST /system/first-boot`, then authenticate the admin user and redirect to admin dashboard
    - _Requirements: 2.1, 2.2, 2.8, 12.3_

  - [x] 13.5 Update frontend environment configuration
    - Add `cognitoDomainUrl`, `cognitoAppClientId`, `cognitoRegion` to environment files (`environment.ts`, `environment.development.ts`, `environment.production.ts`)
    - Update frontend build/deploy scripts to inject Cognito values from SSM parameters
    - _Requirements: 11.1, 13.6_

- [x] 14. Checkpoint - Verify frontend builds and all tests pass
  - Ensure `npm run build` succeeds in `frontend/ai.client/`
  - Ensure all backend and property tests pass
  - Ask the user if questions arise.

- [x] 15. Integration Wiring and SSM Parameter Consumption
  - [x] 15.1 Wire App API Stack to consume Cognito SSM parameters
    - Update `infrastructure/lib/app-api-stack.ts` to import Cognito User Pool ID, App Client ID, Issuer URL, and Domain URL from SSM parameters
    - Pass Cognito configuration as environment variables to the App API ECS task definition (`COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `COGNITO_ISSUER_URL`, `COGNITO_DOMAIN_URL`, `COGNITO_REGION`)
    - _Requirements: 13.4_

  - [x] 15.2 Update GitHub Actions workflows for Cognito context values
    - Add `CDK_COGNITO_DOMAIN_PREFIX` and any other Cognito-related CDK context variables to GitHub Actions workflow files following the existing `CDK_` prefix convention
    - No Cognito secrets required since the User Pool is CDK-managed
    - _Requirements: 13.6_

- [x] 16. Final Checkpoint - Ensure all tests pass
  - Ensure all property-based tests pass (17 properties)
  - Ensure all unit tests pass
  - Ensure CDK synthesizes cleanly
  - Ensure frontend builds successfully
  - Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major phase
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases using pytest
- The implementation order ensures no orphaned code: infrastructure → backend APIs → frontend → removals → wiring
