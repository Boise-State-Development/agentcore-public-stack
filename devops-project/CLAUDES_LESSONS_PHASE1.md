# Lessons Learned: Phase 0 & Phase 1

## Overview
This document captures key learnings, gotchas, and process improvements discovered during Phase 0 (Initialization) and Phase 1 (Frontend Stack) implementation. These insights should inform our approach to Phases 2-5.

---

## Technical Discoveries

### Angular 17+ Breaking Changes
**Issue**: Angular 17+ introduced significant changes that broke our initial implementation:
- New `@angular/build:application` builder replaced the old browser builder
- Output directory structure changed to `dist/<project>/browser/` instead of `dist/`
- Test framework switched from Karma to Vitest
- CLI argument changes: `--watch=false` → `--no-watch`
- Removed arguments: `--code-coverage`, `--no-progress`, `--source-map`
- Bundle size budgets are significantly exceeded with modern libraries (Prism.js, Mermaid, KaTeX)

**Solutions Implemented**:
1. Modified `build.sh` to check multiple possible output locations
2. Updated `test.sh` to use Vitest-compatible arguments
3. Fixed test files to use `provideRouter([])` for routing dependencies
4. Increased bundle budgets to realistic values (2MB warning, 5MB error)

**Lesson**: Always verify framework versions and check for breaking changes in major releases. Build scripts should be defensive and handle multiple output structures.

---

### GitHub Actions Composite Actions Pattern
**Discovery**: Repeated AWS credential configuration code across jobs led to duplication and maintenance burden.

**Solution**: Created reusable composite action at `.github/actions/configure-aws-credentials/action.yml`

**Benefits**:
- Single source of truth for authentication logic
- Automatic OIDC → Access Keys fallback
- 60% reduction in workflow YAML code
- Easier to update authentication logic across all stacks

**Lesson**: Identify common patterns early and abstract them into composite actions. This pattern should be applied to other common workflows (e.g., `setup-node-python`, `deploy-with-cdk`).

---

### CDK Context vs Environment Variables
**Issue**: Hard-coding configuration in `cdk.context.json` creates security risks and reduces portability.

**Solution**: Modified `load-env.sh` to prioritize environment variables over context file:
```bash
export CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-$(get_json_value "projectPrefix" "${CONTEXT_FILE}")}"
```

**GitHub Organization**:
- **Secrets** (sensitive): `CDK_AWS_ACCOUNT`, `CDK_CERTIFICATE_ARN`, AWS credentials
- **Variables** (config): `CDK_PROJECT_PREFIX`, `AWS_REGION`, `CDK_VPC_CIDR`, `CDK_DOMAIN_NAME`

**Lesson**: Always design for configuration injection. Local development can use config files, but CI/CD should override with secrets/variables.

---

### CDK Bootstrap Region Confusion
**Issue**: CDK was deploying to `us-east-1` despite environment variables set to `us-west-2`. The bootstrap resources were cached in the wrong region.

**Solution**: 
1. Pass explicit context flags to `cdk deploy`:
   ```bash
   cdk deploy FrontendStack \
     --context projectPrefix=${CDK_PROJECT_PREFIX} \
     --context awsAccount=${CDK_AWS_ACCOUNT} \
     --context awsRegion=${CDK_AWS_REGION}
   ```
2. Ensure stack names use project prefix for proper identification

**Lesson**: CDK caches lookups aggressively. Always pass explicit context for critical values like region and account. Don't rely solely on environment variables.

---

### Error Handling in Bash Scripts
**Issue**: Scripts with `set -euo pipefail` were exiting before error messages could be displayed.

**Solution**: Temporarily disable error exit for AWS CLI calls:
```bash
set +e
OUTPUT=$(aws ssm get-parameter ... 2>&1)
EXIT_CODE=$?
set -e

if [ $EXIT_CODE -ne 0 ]; then
    log_error "Detailed error: $OUTPUT"
    exit 1
fi
```

**Lesson**: Proper error handling requires temporarily disabling `set -e` to capture and display meaningful error messages. Always re-enable it after error handling.

---

### Package Lock Files in CI/CD
**Discovery**: GitHub Actions cache requires `package-lock.json` to function properly.

**Decision**: Commit all `package-lock.json` files to repository.

**Benefits**:
- Reproducible builds across environments
- Faster CI/CD with proper caching
- Security through lock file integrity checks
- Prevents "works on my machine" issues

**Lesson**: Always commit lock files for Node.js projects. The small repo size increase is worth the reliability gains.

---

### CloudFront Origin Access Control (OAC)
**Discovery**: The older Origin Access Identity (OAI) pattern is deprecated. CDK's `S3Origin` is also deprecated.

**Current Implementation**: Used deprecated `S3Origin` for speed, but generates warnings.

**TODO for Phase 2+**: Migrate to `S3BucketOrigin` from `@aws-cdk-lib/aws-cloudfront-origins` to eliminate deprecation warnings.

**Lesson**: Deprecation warnings should be addressed before they become breaking changes in future major versions.

---

## Process Improvements for Phase 2-5

### 1. Pre-Flight Checks
**Add to each phase**:
- [ ] Verify all required GitHub Secrets are set before starting
- [ ] Run `npm install` and verify no breaking changes in dependencies
- [ ] Check for framework version mismatches early
- [ ] Review deprecation warnings in existing stacks before proceeding

### 2. Testing Strategy
**Current Gap**: Tests pass but only check component instantiation, not actual functionality.

**Improvement**:
- Add integration tests that verify actual component behavior
- Test routing configurations
- Mock external dependencies (APIs, services)
- Add visual regression testing for critical UI components

**Implementation**: Consider creating a separate testing workflow that runs more comprehensive tests on a schedule.

### 3. Script Validation
**Pattern to Follow**:
```bash
# Always start with
set -euo pipefail

# For commands that may fail
set +e
RESULT=$(command 2>&1)
EXIT_CODE=$?
set -e

# Always provide context in errors
if [ $EXIT_CODE -ne 0 ]; then
    log_error "What failed"
    log_error "Actual error: $RESULT"
    log_error "Possible causes:"
    log_error "  1. Specific cause"
    log_error "  2. Another cause"
    exit 1
fi
```

**Lesson**: Defensive scripting prevents hours of debugging in CI/CD.

### 4. Incremental Deployment Testing
**Current Issue**: Had to comment out deployment conditions to test on PR branches.

**Better Approach**:
- Add `workflow_dispatch` with environment selection (dev/staging/prod)
- Create separate workflows for PR validation vs. deployment
- Use branch protection rules to enforce deployment gates

**Example Structure**:
```yaml
on:
  pull_request:  # Build and test only
  push:
    branches: [main, develop]  # Full deployment
  workflow_dispatch:  # Manual deployment with environment selection
    inputs:
      environment:
        type: choice
        options: [dev, staging, prod]
```

### 5. Stack Naming Conventions
**Discovered**: Stack names must be predictable and include project prefix.

**Standard Pattern**:
```typescript
stackName: `${config.projectPrefix}-${StackName}Stack`
```

**Example**: `agentcore-FrontendStack`, `agentcore-AppApiStack`

**Lesson**: Consistency in naming makes cross-stack references and debugging much easier.

### 6. SSM Parameter Store Strategy
**Current Pattern**: Store outputs in SSM for cross-stack references.

**Path Convention**:
```
/${projectPrefix}/${stackName}/${resourceName}
```

**Examples**:
- `/${projectPrefix}/frontend/bucket-name`
- `/${projectPrefix}/frontend/distribution-id`
- `/${projectPrefix}/network/vpc-id`
- `/${projectPrefix}/network/private-subnet-ids`

**Lesson**: Establish naming conventions early. They're hard to change once stacks are deployed.

### 7. Documentation During Development
**Gap**: Had to retroactively document lessons learned.

**Better Approach**:
- Document decisions as they're made
- Add inline comments explaining non-obvious choices
- Update THE_PLAN.md immediately after completing tasks
- Create decision records for architectural choices

### 8. Workflow File Organization
**For Phase 2-5, consider**:
- Shared job definitions using reusable workflows
- Common steps abstracted into more composite actions
- Environment-specific configuration files

**Potential Composite Actions**:
- `setup-cdk-environment` (Node.js + Python + AWS CLI)
- `build-and-push-docker` (for ECS stacks)
- `deploy-cdk-stack` (standardized deployment logic)

### 9. Cost Optimization Checkpoints
**Add to each phase**:
- Review AWS resource costs before deploying
- Set up billing alerts
- Use smaller instance sizes for dev/staging
- Consider AWS CDK aspects for automatic tagging and cost allocation

### 10. Rollback Strategy
**Currently Missing**: No automated rollback on deployment failure.

**Recommendation for Phase 2+**:
- Implement CloudFormation rollback triggers
- Save previous CloudFormation templates
- Document manual rollback procedures
- Consider blue-green deployment for zero-downtime updates

---

## Phase 2+ Checklist Template

Before starting each phase:
- [ ] Review lessons from previous phases
- [ ] Verify all GitHub Secrets/Variables are configured
- [ ] Check for dependency updates and breaking changes
- [ ] Create composite actions for repeated patterns
- [ ] Document architectural decisions as you go
- [ ] Test scripts locally before committing
- [ ] Use explicit context flags in CDK commands
- [ ] Implement proper error handling with meaningful messages
- [ ] Add SSM parameters for cross-stack references
- [ ] Test deployment in dev environment first
- [ ] Update THE_PLAN.md immediately after task completion
- [ ] Verify stack names follow naming convention

---

## Open Questions for Phase 2

1. **Database Strategy**: DynamoDB vs RDS Aurora - which for App API?
   - Consider access patterns, cost, and operational overhead
   
2. **Container Registry**: ECR per service or shared registry?
   - Recommendation: Separate ECR repos for isolation and access control

3. **VPC Design**: Public/private subnet split, NAT Gateway costs
   - Consider using VPC endpoints to reduce NAT Gateway traffic

4. **ECS Service Discovery**: Use AWS Cloud Map or ALB-based routing?
   - Recommendation: Cloud Map for service-to-service, ALB for external

5. **Secrets Management**: How to inject secrets into ECS tasks?
   - Use AWS Secrets Manager with ECS task execution role

6. **Monitoring Strategy**: CloudWatch vs third-party (Datadog, etc.)?
   - Start with CloudWatch, evaluate cost/benefit later

---

## Success Metrics

**Phase 1 Achievements**:
- ✅ Full CI/CD pipeline operational
- ✅ Reusable composite action created
- ✅ Configuration externalized to secrets/variables
- ✅ All tests passing
- ✅ Frontend deployable to any AWS region
- ✅ Cross-stack references via SSM working

**Target for Phase 2**:
- Zero-downtime deployments
- Automated rollback on failure
- Database migration strategy
- Service-to-service authentication
- Comprehensive health checks

---

## Conclusion

Phase 1 was a learning phase where we discovered many gotchas related to modern tooling (Angular 17+, CDK patterns, GitHub Actions). The key takeaway is **defensive programming** - anticipate failures, provide detailed error messages, and make configuration explicit rather than implicit.

The composite action pattern and environment variable prioritization will serve us well in Phases 2-5. We should continue to identify common patterns and abstract them early to avoid technical debt.

Most importantly: **Document as you go, not after the fact.**
