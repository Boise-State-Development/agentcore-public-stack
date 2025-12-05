# Lessons Learned: Phase 2 - App API Stack

## Overview
This document captures **real issues, gotchas, and solutions discovered while testing and deploying** Phase 2 (App API Stack). 

**Important**: This file should be updated **as we encounter problems together** during testing, not pre-filled with assumptions. It's a living document that grows through experience.

---

## Technical Discoveries

### Python Import Paths in FastAPI Applications
**Issue**: The App API `main.py` was using absolute imports (`from health.health import router`) instead of relative imports, causing `ModuleNotFoundError` when the application started.

**Root Cause**: When a FastAPI application is structured as a package with submodules, imports must use relative imports (`.health.health`) or the module must have proper `__init__.py` files to expose the desired imports.

**Solution**: 
1. Changed imports in `main.py` from `from health.health import router` to `from .health.health import router`
2. Created `__init__.py` in the `health/` directory to expose the router: `from .health import router`
3. Updated import to cleaner path: `from .health import router`

**Lesson**: Always use relative imports for sibling modules in package structures. Create `__init__.py` files to expose public APIs from subpackages for cleaner import paths.

---

### Angular Testing with HTTP Dependencies
**Issue**: Frontend tests were failing with `HttpErrorResponse: Http failure response for http://localhost:8000/sessions: 0 Unknown Error` because Angular's `resource()` API was trying to make real HTTP requests during tests.

**Root Cause**: Tests didn't provide HTTP client testing providers, so Angular attempted to make actual network calls to the backend API which wasn't running.

**Solution**: Added `provideHttpClient()` and `provideHttpClientTesting()` to all test configurations:
```typescript
providers: [
  provideRouter([]),
  provideHttpClient(),
  provideHttpClientTesting()
]
```

**Lesson**: Always provide HTTP testing utilities in Angular tests when components/services use `HttpClient` or the new `resource()` API. This mocks HTTP requests automatically.

---

### Docker Build Context and Editable Installs
**Issue**: Docker build was failing with error `error in 'egg_base' option: 'src' does not exist or is not a directory` when trying to install Python package with `pip install -e .`

**Root Cause**: The Dockerfile was attempting an editable install (`pip install -e .`) before copying the `src/` directory into the image. Editable installs require the source code to be present.

**Solution**: Changed approach to install dependencies directly instead of using editable mode:
```dockerfile
# Copy pyproject.toml to install dependencies
COPY backend/pyproject.toml ./

# Install Python dependencies only (extract from pyproject.toml)
RUN pip install --no-cache-dir fastapi==0.116.1 uvicorn[standard]==0.35.0 ...
```

**Lesson**: For Docker images, avoid editable installs. Either:
1. Install dependencies explicitly (preferred for production)
2. Copy source code before installing if you need to install the package itself
3. Use multi-stage builds to separate dependency installation from runtime code

---

### Bash Function Availability After Sourcing Scripts
**Issue**: Build and deploy scripts were calling `load_configuration` function which didn't exist, causing `command not found` errors.

**Root Cause**: The `load-env.sh` script was refactored to run configuration loading automatically when sourced, removing the `load_configuration()` function. However, other scripts still had calls to this removed function.

**Solution**: Removed `load_configuration` function calls from `build.sh` and `deploy.sh` since configuration is now loaded automatically when `load-env.sh` is sourced.

**Lesson**: When refactoring shared utility scripts, search for all usages of removed functions across the codebase. In bash, functions must be explicitly sourced before they can be called.

---

### Docker Container Startup Timing in Tests
**Issue**: Docker health check tests were failing immediately with "Container exited unexpectedly" even though the container was running fine and returning 200 status codes.

**Root Cause**: The test script was checking if the container was running and attempting health checks in the wrong order - it didn't give the container any time to initialize before the first check. The logic was:
```bash
if docker ps | grep -q "${CONTAINER_ID}"; then
    # Try health check immediately
```

This would fail on the first iteration because the container hadn't started yet, then immediately check if it was still running and report it as exited.

**Solution**: Restructured the test logic to:
1. Give container an initial 3-second grace period after starting
2. Check health endpoint first
3. Only check if container exited after health check fails
4. Sleep between iterations

**Lesson**: Container startup takes time. Always provide an initial grace period before running health checks, and structure test loops to prioritize the success case (health check passes) over the failure case (container crashed).

---

### Script Dependencies: Only Source What You Need
**Issue**: The `test-docker.sh` script was sourcing `load-env.sh` which validates full AWS configuration including `CDK_AWS_ACCOUNT`. This caused the test to fail in CI/CD with "AWS Account ID is required" even though Docker testing doesn't need AWS credentials.

**Root Cause**: Blindly sourcing shared utility scripts without considering what configuration is actually needed. The Docker test only needs `CDK_PROJECT_PREFIX` to construct the image name, not full AWS configuration.

**Solution**: Removed the `source load-env.sh` line and set `CDK_PROJECT_PREFIX` directly from environment with a fallback:
```bash
CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-agentcore}"
```

**Benefits**:
- Script works without AWS credentials configured
- Faster execution (no unnecessary validation)
- Clearer dependencies (explicit about what's needed)
- Can run locally without any setup

**Lesson**: Don't automatically source shared utility scripts in every script. Consider what each script actually needs. Testing scripts should have minimal dependencies and work without full environment configuration when possible.

---

## Gotchas and Workarounds

### GitHub Secrets Must Be Explicitly Passed to Workflow Steps
**Issue**: Build step was failing with `AWS Account ID is required` error despite GitHub Secrets being configured in the repository.

**Root Cause**: GitHub Secrets are **not automatically available** as environment variables. They must be explicitly passed using the `env:` key in each workflow step that needs them.

**Solution**: Added environment variables to the build step in the workflow:
```yaml
- name: Build Docker image
  env:
    CDK_AWS_ACCOUNT: ${{ secrets.CDK_AWS_ACCOUNT }}
    CDK_AWS_REGION: ${{ env.AWS_REGION }}
  run: |
    chmod +x scripts/stack-app-api/build.sh
    scripts/stack-app-api/build.sh
```

**Lesson**: Always explicitly pass secrets and variables to steps that need them. Job-level `env:` declarations don't automatically propagate secrets - you must reference them explicitly with `${{ secrets.SECRET_NAME }}`.

---

### Python pyproject.toml License Format Deprecation
**Issue**: Docker build showed deprecation warning: `project.license as a TOML table is deprecated`

**Root Cause**: The `pyproject.toml` used old license format:
```toml
license = {text = "MIT"}
```

**Current Status**: Warning only, not blocking. Modern format should be:
```toml
license = "MIT"
```

**TODO**: Update `pyproject.toml` to use modern SPDX license string format before setuptools deprecation deadline (2026-Feb-18).

**Lesson**: Monitor deprecation warnings in build logs. They indicate future breaking changes that should be addressed proactively.

---

### CDK Deprecation Warnings to Address
**Warnings Observed During Deployment**:

1. **S3Origin is deprecated** (Frontend Stack)
   - Current: `aws_cloudfront_origins.S3Origin`
   - Replacement: Use `S3BucketOrigin` or `S3StaticWebsiteOrigin`
   - Files: `infrastructure/lib/frontend-stack.ts`

2. **pointInTimeRecovery is deprecated** (App API Stack)
   - Current: `pointInTimeRecovery: true`
   - Replacement: Use `pointInTimeRecoverySpecification` instead
   - Files: `infrastructure/lib/app-api-stack.ts`

3. **containerInsights is deprecated** (App API Stack)
   - Current: `containerInsights: true`
   - Replacement: Use `containerInsightsV2` instead
   - Files: `infrastructure/lib/app-api-stack.ts`

**Status**: Non-blocking warnings. Stack deploys successfully, but these should be addressed before the next major CDK release.

**Action Items for Future**:
- [ ] Update frontend stack to use `S3BucketOrigin` from `@aws-cdk/aws-cloudfront-origins`
- [ ] Update DynamoDB table config to use `pointInTimeRecoverySpecification`
- [ ] Update ECS cluster config to use `containerInsightsV2`

**Lesson**: CDK deprecation warnings accumulate across phases. Document them as they appear and batch-fix them periodically to avoid technical debt buildup.

---

### GitHub Actions Rule Compliance: No Inline Logic
**Issue**: Initially implemented Docker health check testing as inline bash script in the GitHub Actions workflow YAML, violating the project's "No Inline Logic in YAML" rule.

**Rule from CLAUDES_INSTRUCTIONS.md**: 
> GitHub Actions workflows must ONLY call shell scripts. No `run: npm install` or `run: aws s3 sync` inside the YAML. The ONLY exception is installing base dependencies (e.g., `actions/setup-node@v3`) and calling the shell scripts.

**Solution**: Extracted all Docker testing logic into `scripts/stack-app-api/test-docker.sh` and updated workflow to simply call the script.

**Benefits**:
- Scripts are testable locally without CI/CD
- Logic is portable across different CI/CD platforms
- Easier to debug and iterate on test logic
- Consistent with project architecture principles

**Lesson**: Always review project constraints before implementing. When testing/debugging in CI/CD, it's tempting to add inline logic for speed, but maintaining architectural consistency pays off in maintainability.

---

### OIDC Authentication Requires Explicit Permissions
**Issue**: AWS credentials configuration was failing with "Credentials could not be loaded" even though the composite action was configured correctly. The error message hinted: "It looks like you might be trying to authenticate with OIDC. Did you mean to set the `id-token` permission?"

**Root Cause**: GitHub Actions OIDC authentication requires explicit `permissions:` block in the job that uses it. Without `id-token: write` permission, the OIDC token cannot be generated and AWS authentication fails, even when falling back to access keys.

**Solution**: Added permissions block to the deploy job:
```yaml
permissions:
  id-token: write
  contents: read
```

**Why This Matters**:
- OIDC provides more secure authentication than long-lived access keys
- Permissions must be explicitly granted at the job level (not inherited)
- Without proper permissions, authentication silently fails with cryptic error messages

**Lesson**: When copying workflow patterns across jobs/files, always include the complete configuration including permissions blocks. OIDC authentication is not just about the action configuration - it requires job-level permissions to function.

---

## Process Improvements

### Consistent Secret Naming Across Stacks
**Discovery**: Reusing the same GitHub Secret names across multiple stacks simplifies configuration management.

**Implementation**: Both frontend and app-api workflows use identical secret names:
- `CDK_AWS_ACCOUNT` - AWS account ID (shared across all stacks)
- `AWS_ROLE_ARN` - For OIDC authentication (shared)
- `AWS_ACCESS_KEY_ID` - Fallback authentication (shared)
- `AWS_SECRET_ACCESS_KEY` - Fallback authentication (shared)

**Benefits**:
- No duplicate secrets to manage
- Single configuration for all stacks
- Easier to update credentials centrally
- Reduces human error in secret configuration

**Lesson**: Establish naming conventions for secrets/variables early and reuse them across all workflows. This is especially important for shared resources like AWS credentials.

---

### Test Script Design Pattern
**Discovery**: Test scripts should handle missing test directories gracefully and provide basic smoke tests as fallback.

**Pattern Implemented** in `scripts/stack-app-api/test.sh`:
```bash
if [ -d "tests" ]; then
    # Run full test suite with pytest
    python3 -m pytest tests/ -v
else
    # Fallback to basic import check
    python3 -c "from apis.app_api.main import app"
    python3 -c "from apis.app_api.health import router"
fi
```

**Benefits**:
- Tests don't fail when test directory doesn't exist yet
- Provides minimum validation (import checks)
- Encourages creating proper tests later
- Prevents CI/CD blocking during initial development

**Lesson**: Design scripts to be progressive - work with minimal setup initially, but encourage/support more robust validation as the project matures.

---

### Dockerfile Dependency Management Strategy
**Discovery**: For Docker images, explicitly listing dependencies is more maintainable than installing from `pyproject.toml`.

**Rationale**:
1. Clearer what's actually being installed
2. Avoids editable install complications
3. Faster builds (no need to copy source for dependency resolution)
4. Better layer caching

**Trade-off**: Requires manual sync between `pyproject.toml` and `Dockerfile` dependencies.

**Recommendation for Phase 3+**: Consider using `pip install --no-deps` with a requirements.txt generated from pyproject.toml, or use a two-stage approach where dependencies are resolved separately from the package installation.

**Lesson**: Different deployment targets (local dev vs Docker vs Lambda) may require different dependency installation strategies. Choose based on your constraints (build time, image size, maintainability).

---

## Open Questions

### Database Selection for App API
**Question**: Should App API use DynamoDB or RDS Aurora Serverless v2?

**Current Implementation**: CDK stack supports both via configuration flag.

**Considerations**:
- **DynamoDB**: Lower cost, serverless, better for key-value access patterns
- **RDS Aurora**: SQL queries, better for relational data, familiar tooling

**Decision Needed**: Based on actual data access patterns of the application.

---

### ECS Task Sizing
**Question**: What are the appropriate CPU/memory allocations for App API tasks?

**Current Configuration**: Using CDK defaults (likely 256 CPU / 512 MB)

**TODO**: Monitor actual resource usage after deployment and adjust based on real metrics.

---

### Health Check Configuration
**Question**: Are the current health check intervals and timeouts optimal?

**Current Settings**: 
- Dockerfile: `--interval=30s --timeout=5s --start-period=60s --retries=3`
- Workflow test: 30 second timeout with 2 second intervals

**TODO**: Validate these settings work well with actual application startup time and load.

---

### ECR Image Lifecycle Policy
**Question**: Should we implement automatic cleanup of old ECR images?

**Current State**: No lifecycle policy configured.

**Consideration**: ECR storage costs can accumulate with many image versions. Consider implementing policy to retain only last N images or images from last X days.

**Recommendation**: Add lifecycle policy in Phase 3+ to manage costs.

---

## Notes for Future Phases

### Patterns to Reuse in Phase 3 (Inference API)

1. **Dockerfile Structure**: The App API Dockerfile pattern (multi-stage build, explicit dependencies, runtime code copy) should be replicated for Inference API.

2. **Test Script Pattern**: Use the same fallback pattern (try pytest, fallback to import checks) for Inference API tests.

3. **Workflow Structure**: The app-api.yml workflow can serve as a template for inference-api.yml with minimal changes:
   - Different paths for triggers
   - Different Docker image name
   - Same secret/variable references
   - Same deployment pattern

4. **Import Path Consistency**: Ensure Inference API also uses relative imports and proper `__init__.py` files from the start.

5. **Environment Variables**: Continue using the same secret names (`CDK_AWS_ACCOUNT`, etc.) for consistency.

---

### Cross-Stack Integration Points

For Phase 3, the Inference API will need to:
1. Import VPC ID from SSM Parameter Store (already exported by App API stack)
2. Import ALB ARN and Listener ARN from SSM
3. Add a new listener rule to route `/inference/**` to Inference API target group

**Key Learning**: The SSM parameter naming convention established in Phase 1-2 makes cross-stack references straightforward.

---

### Docker Build Optimization Opportunities

**Current Approach**: Installing dependencies on every build.

**Potential Optimization**: 
- Use Docker layer caching more effectively
- Consider building a base image with common dependencies
- Use buildx cache mounts for pip cache

**Trade-off**: More complexity vs faster builds. Evaluate based on CI/CD build times after Phase 3.

---

### Testing Gaps to Address

**What's Working**: 
- Import checks validate basic code structure
- Docker container health checks validate runtime startup

**What's Missing**:
- No actual API endpoint testing
- No integration tests with database
- No load testing

**Recommendation**: After Phase 3, create comprehensive integration test suite that:
1. Spins up Docker containers locally
2. Tests actual API endpoints
3. Validates database connections
4. Tests inter-service communication

---

### Secret Management Strategy

**Current Approach**: GitHub Secrets for deployment credentials, AWS Secrets Manager for runtime secrets.

**Working Well**: 
- Clear separation of deployment vs runtime secrets
- Reusable secret names across workflows

**TODO for Phase 3+**:
- Document which secrets are needed for each stack
- Create a secrets checklist for new deployments
- Consider using AWS Systems Manager Parameter Store for non-sensitive configuration that needs to be shared across services

---

### Cost Monitoring Recommendations

**Phase 2 Resources That Cost Money**:
- NAT Gateways (most expensive - ~$32/month per AZ)
- ALB (~$16/month + data processing)
- Fargate tasks (depends on running time and size)
- RDS Aurora (if enabled, serverless v2 charges per ACU)
- ECR storage (per GB)

**Action Items**:
1. Set up AWS Budget alerts before deploying Phase 3
2. Tag all resources with project name for cost allocation
3. Consider using VPC endpoints to reduce NAT Gateway data transfer costs
4. Review whether both AZs are necessary for dev/staging environments

---

### Lessons Applied from Phase 1

✅ **Used explicit context flags in CDK deploy commands**
✅ **Proper error handling in bash scripts**
✅ **Environment variable prioritization over context file**
✅ **Composite action for AWS credentials**
✅ **Consistent SSM parameter naming convention**
✅ **Defensive scripting with meaningful error messages**

**Result**: Phase 2 implementation was smoother due to lessons learned in Phase 1. Continue applying these patterns in Phase 3+.
