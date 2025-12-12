# Phase 2 Lessons: App API Stack Deployment

**Purpose**: Critical issues and solutions from Phase 2 testing/deployment. Updated as problems are encountered.

---

## Core Technical Issues

### 1. Python Imports in FastAPI
- **Problem**: `ModuleNotFoundError` with absolute imports (`from health.health`)
- **Fix**: Use relative imports (`.health.health`) + `__init__.py` to expose public APIs
- **Rule**: Always use relative imports for sibling modules in packages

### 2. Angular HTTP Testing
- **Problem**: Tests fail with real HTTP requests when using `resource()` API
- **Fix**: Add `provideHttpClient()` and `provideHttpClientTesting()` to test configs
- **Rule**: Always mock HTTP in Angular tests

### 3. Docker Editable Installs
- **Problem**: `pip install -e .` fails before `src/` is copied
- **Fix**: Install dependencies explicitly or copy source first
- **Rule**: Avoid editable installs in Docker; use explicit dependencies

### 4. Bash Script Refactoring
- **Problem**: `load_configuration` function removed but still called elsewhere
- **Fix**: Search all usages when removing shared functions
- **Rule**: Grep for function usage before deleting from shared scripts

### 5. Docker Health Check Timing
- **Problem**: Tests fail immediately without grace period
- **Fix**: 3-second initial delay, then check health before checking container status
- **Rule**: Always provide startup grace period for containers

### 6. Script Dependencies
- **Problem**: `test-docker.sh` sourced `load-env.sh` requiring AWS config unnecessarily
- **Fix**: Set only needed vars directly: `CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-agentcore}"`
- **Rule**: Test scripts should have minimal dependencies; don't blindly source utilities

---

## GitHub Actions & AWS

### 7. Secrets in Workflows
- **Problem**: Secrets configured but not available in steps
- **Fix**: Explicitly pass to each step: `env: CDK_AWS_ACCOUNT: ${{ secrets.CDK_AWS_ACCOUNT }}`
- **Rule**: Job-level env doesn't auto-propagate secrets; reference explicitly

### 8. OIDC Permissions
- **Problem**: "Credentials could not be loaded" despite correct action config
- **Fix**: Add to job: `permissions: { id-token: write, contents: read }`
- **Rule**: OIDC requires explicit job-level permissions

### 9. No Inline Logic Rule
- **Rule**: GitHub Actions YAML must only call scripts (except setup actions)
- **Benefit**: Testable locally, portable, easier debugging
- **Applied**: Extracted Docker test logic to `test-docker.sh`

---

## Deprecations to Address

### pyproject.toml License (Non-blocking)
- Change `license = {text = "MIT"}` to `license = "MIT"`
- Deadline: 2026-Feb-18

### CDK Warnings (Non-blocking)
1. **S3Origin** → Use `S3BucketOrigin` in `frontend-stack.ts`
2. **pointInTimeRecovery** → Use `pointInTimeRecoverySpecification` in `app-api-stack.ts`
3. **containerInsights** → Use `containerInsightsV2` in `app-api-stack.ts`


---

## Best Practices Established

### Consistent Naming
- Reuse GitHub secret names across stacks: `CDK_AWS_ACCOUNT`, `AWS_ROLE_ARN`, etc.
- Benefits: Single config point, no duplicates, easier updates

### Test Script Pattern
```bash
# Fallback pattern for progressive validation
if [ -d "tests" ]; then
    python3 -m pytest tests/ -v
else
    python3 -c "from apis.app_api.main import app"  # Smoke test
fi
```
- Works with minimal setup, encourages proper tests later

### Dockerfile Dependencies
- Explicit listing > installing from pyproject.toml for Docker
- Trade-off: Manual sync needed, but clearer + faster builds
- Alternative: Two-stage approach with requirements.txt generation

---

## Phase 3 Reuse Patterns

1. **Dockerfile**: Multi-stage build, explicit deps, runtime code copy
2. **Tests**: pytest with import check fallback
3. **Workflow**: Same structure, different paths/image names, reuse secrets
4. **Imports**: Relative imports + `__init__.py` from start
5. **Integration**: VPC/ALB references via SSM (convention already established)

---

## Outstanding Items

### Open Questions
- **Database**: DynamoDB vs RDS Aurora? (depends on access patterns)
- **ECS sizing**: Monitor actual usage post-deploy (currently CDK defaults)
- **Health checks**: Validate 30s interval/60s startup optimal
- **ECR lifecycle**: Consider cleanup policy to manage costs

### Testing Gaps
- ✅ Import validation, Docker health checks
- ❌ Endpoint testing, DB integration, load testing
- **TODO**: Integration suite after Phase 3 (spin up containers, test APIs)

### Cost Management
**Main costs**: NAT Gateways (~$32/mo/AZ), ALB (~$16/mo), Fargate, ECR storage
**Actions**: Budget alerts, resource tagging, review AZ count for dev/staging, consider VPC endpoints
