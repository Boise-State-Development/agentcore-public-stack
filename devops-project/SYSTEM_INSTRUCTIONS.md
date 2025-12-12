# System Instructions for AWS CDK Multi-Stack Deployment

## Project Architecture

**5-Stack AWS CDK System**:
1. **Infrastructure Stack** - Foundation layer (VPC, ALB, ECS Cluster)
2. **Frontend Stack** - S3 + CloudFront + Route53
3. **App API Stack** - Fargate service for application API
4. **Inference API Stack** - Fargate service for inference workloads
5. **Agent Core/Gateway Stacks** - Lambda/Step Functions/API Gateway

**Technology**: TypeScript (CDK), Python/FastAPI (backends), Angular 17+ (frontend), Bash scripts (CI/CD)

---

## Core Principles

### 1. Configuration Management
- **NEVER hardcode**: AWS Account IDs, regions, resource names, ARNs
- **Prioritization**: Environment variables > Context file > Defaults
- **GitHub Secrets** (sensitive): `CDK_AWS_ACCOUNT`, `CDK_CERTIFICATE_ARN`, AWS credentials
- **GitHub Variables** (config): `CDK_PROJECT_PREFIX`, `AWS_REGION`, `CDK_VPC_CIDR`, `CDK_DOMAIN_NAME`
- **Pass explicit context** to CDK: `--context projectPrefix=... --context awsAccount=... --context awsRegion=...`
- **SSM for runtime state**: Store dynamic values (image tags) in SSM, not context

### 2. Shell Scripts First
- **Rule**: GitHub Actions YAML must ONLY call shell scripts (except setup actions like `actions/setup-node`)
- **No inline logic**: Never `run: npm install` or `run: aws s3 sync` in YAML
- **Benefits**: Testable locally, portable, easier debugging
- **Portability**: Use `/bin/bash`, work on ubuntu-latest/macOS/WSL
- **Error handling**: Always use `set -euo pipefail` with proper error capture

### 3. Stack Layering Architecture
**Critical**: Separate shared infrastructure from application-specific resources

- **Foundation Layer (Infrastructure Stack)**:
  - VPC, ALB, ECS Cluster, Security Groups
  - SSM parameters for cross-stack sharing
  - Deploy FIRST, always
  
- **Application Layer (Frontend/API Stacks)**:
  - Service-specific resources only
  - Import foundation via SSM parameters
  - Deploy after Infrastructure Stack

**Deployment Order**: Infrastructure → App API → Inference API → Frontend

### 4. Cross-Stack References
- **Use SSM Parameter Store** for all cross-stack resource sharing
- **Never hardcode** ARNs or resource IDs
- **Naming convention**: `/${projectPrefix}/${category}/${resourceName}`
  - Network: `/${projectPrefix}/network/vpc-id`
  - Services: `/${projectPrefix}/app-api/image-tag`
  - Frontend: `/${projectPrefix}/frontend/distribution-id`

---

## Critical Technical Patterns

### CDK Resource Imports
```typescript
// VPC - Use fromVpcAttributes() NOT fromLookup() (Tokens incompatible)
const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
  vpcId: vpcId,                                    // Token from SSM
  vpcCidrBlock: vpcCidr,                          // Token from SSM
  availabilityZones: cdk.Fn.split(',', azString), // Token array
  privateSubnetIds: cdk.Fn.split(',', subnetIds), // Token array
});

// Security Groups - Use fromSecurityGroupId()
// ECS Clusters - Use fromClusterAttributes()
// ALBs - Use fromApplicationLoadBalancerAttributes()
```

### Python Import Rules
```python
# WRONG - Absolute imports cause ModuleNotFoundError
from health.health import router

# CORRECT - Relative imports within packages
from .health.health import router
from .admin.routes import router as admin_router
```

### Bash Error Handling Pattern
```bash
set -euo pipefail

# For commands that may fail
set +e
OUTPUT=$(command 2>&1)
EXIT_CODE=$?
set -e

if [ $EXIT_CODE -ne 0 ]; then
    log_error "What failed"
    log_error "Actual error: $OUTPUT"
    log_error "Possible causes:"
    log_error "  1. Specific cause"
    log_error "  2. Another cause"
    exit 1
fi
```

### Docker Multi-Stage Build (Python Projects)
```dockerfile
FROM python:3.11-slim as builder

# Copy pyproject.toml and source code for installation
COPY backend/pyproject.toml backend/README.md ./
COPY backend/src ./src

# Install from pyproject.toml (single source of truth)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[agentcore]"  # Include optional deps

FROM python:3.11-slim
# Copy installed packages, then copy runtime code
```

**Key Points**:
- Avoid editable installs (`pip install -e .`) in Docker
- Copy src directory before `pip install .` (required for src-layout packages)
- Use `pip install ".[optional]"` for optional dependencies
- Single source of truth: `pyproject.toml`

---

## GitHub Actions Best Practices

### Workflow Structure (4-Job Pattern)
```yaml
jobs:
  install:
    # Install dependencies, cache node_modules
  test:
    needs: install
    # Restore cache, run tests
  build:
    needs: test
    # Restore cache, build artifacts (Docker/dist)
  deploy:
    needs: build
    # Restore cache/artifacts, deploy to AWS
```

**Benefits**: Clear separation, efficient caching, fail fast, skip tests option

### Workflow Naming Convention
- Format: `<StackName>.BuildTest.Deploy`
- Examples: `InfrastructureStack.BuildTest.Deploy`, `FrontendStack.BuildTest.Deploy`

### Secrets & Permissions
```yaml
jobs:
  deploy:
    permissions:
      id-token: write    # Required for OIDC
      contents: read
    env:
      CDK_AWS_ACCOUNT: ${{ secrets.CDK_AWS_ACCOUNT }}  # Explicit reference required
```

**Rule**: Job-level env doesn't auto-propagate secrets; reference explicitly in each step

### Composite Actions
Create reusable patterns at `.github/actions/<action-name>/action.yml`:
- `configure-aws-credentials` (OIDC → Access Keys fallback)
- Potential: `setup-cdk-environment`, `build-and-push-docker`, `deploy-cdk-stack`

**Benefits**: 60% code reduction, single source of truth, easier updates

### Caching Behavior
- Cache keys based on `package-lock.json` hash
- "Failed to save cache" = **normal** when dependencies unchanged (reusing existing cache)
- Ensure install scripts create what's being cached (e.g., CDK dependencies)

---

## Stack-Specific Technical Requirements

### Infrastructure Stack
- Creates: VPC (2 AZs), ALB with HTTP listener, ECS Cluster, Security Groups
- Exports via SSM: All network resources for app stacks
- Must deploy FIRST

### Frontend Stack (Angular 17+)
- **Breaking changes**: New build system, output to `dist/<project>/browser/`
- **Test framework**: Vitest (not Karma)
- **CLI arguments**: `--no-watch` (not `--watch=false`)
- **Bundle budgets**: 2MB warning, 5MB error (modern libraries)
- **Routing in tests**: Always include `provideRouter([])` + `provideHttpClient()`/`provideHttpClientTesting()`

### API Stacks (Fargate)
- **Import**: VPC, ALB, Cluster from Infrastructure Stack via SSM
- **Image tags**: Store in SSM after ECR push, read during deployment
- **Health checks**: 3-second startup delay, 30s interval, 60s timeout
- **Path routing**: Different priorities (App API: 1, Inference API: 10)

### ECR Lifecycle Policy
```json
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Keep protected tags",
      "selection": {
        "tagStatus": "tagged",
        "tagPrefixList": ["latest", "deployed", "prod", "staging", "v", "release"]
      }
    },
    {
      "rulePriority": 2,
      "description": "Delete untagged after 7 days",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 7
      }
    }
  ]
}
```

**Note**: Cannot use empty string in `tagPrefixList` - AWS validation error

---

## AWS CLI & SSM Operations

### SSM Parameter Operations
```bash
# Writing (no --tags with --overwrite)
aws ssm put-parameter \
    --name "/${CDK_PROJECT_PREFIX}/app-api/image-tag" \
    --value "${IMAGE_TAG}" \
    --type "String" \
    --overwrite \
    --region "${CDK_AWS_REGION}"

# Tags are immutable on existing parameters
# Use AddTagsToResource API separately if needed
```

---

## Testing Strategy

### Test Script Pattern
```bash
# Progressive validation with fallback
if [ ! -d "tests" ] || [ -z "$(ls -A tests/*.test.* 2>/dev/null)" ]; then
    log_info "No tests found, skipping"
    exit 0
fi

# Run actual tests
python3 -m pytest tests/ -v
```

### Docker Health Check Pattern
```bash
# 3-second grace period
sleep 3

# Check health endpoint before container status
if ! curl -f http://localhost:8000/health; then
    docker logs test-container
    exit 1
fi
```

### Minimal Test Dependencies
- Don't source full `load-env.sh` unnecessarily
- Set only required vars: `CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-agentcore}"`
- Keep test scripts minimal - no heavy application dependencies

---

## Naming Conventions

### Stack Names
```typescript
stackName: `${config.projectPrefix}-${StackName}Stack`
// Examples: agentcore-InfrastructureStack, agentcore-FrontendStack
```

### SSM Parameters
```
/${projectPrefix}/network/vpc-id
/${projectPrefix}/network/vpc-cidr
/${projectPrefix}/network/alb-arn
/${projectPrefix}/network/alb-listener-arn
/${projectPrefix}/network/ecs-cluster-name
/${projectPrefix}/network/private-subnet-ids
/${projectPrefix}/network/public-subnet-ids
/${projectPrefix}/network/availability-zones
/${projectPrefix}/app-api/image-tag
/${projectPrefix}/inference-api/image-tag
/${projectPrefix}/frontend/bucket-name
/${projectPrefix}/frontend/distribution-id
```

---

## Workflow & Execution Model

### Task Management
1. **Read** `THE_PLAN.md` to understand current state
2. **Review** `CLAUDES_LESSONS_PHASE*.md` for prior learnings
3. **Identify** first unchecked task (`- [ ]`)
4. **Execute** task (CDK code → Shell scripts → GitHub Actions)
5. **Verify** implementation works
6. **Update** `THE_PLAN.md` immediately: `- [ ]` → `- [x]`

### Documentation Policy
- **DO NOT** create new Markdown files unless explicitly requested
- **ONLY update** `README.md` for factual inaccuracies
- **Update** `THE_PLAN.md` checkboxes after completing tasks
- **Document as you go**, not retroactively

### Lessons Learned Protocol
- Create `CLAUDES_LESSONS_PHASE<N>.md` at phase start with **empty sections**
- Update **ONLY when human encounters real issues during testing**
- Do NOT pre-fill with assumptions or implementation summaries
- Living document that grows through actual experience

### Quality Standards
- Follow TypeScript/Python best practices
- All shell scripts executable with proper error handling
- All configuration externalized (no hardcoded values)
- GitHub Actions workflows only call shell scripts
- Commit all `package-lock.json` files for reproducibility

---

## Common Gotchas & Solutions

| Issue | Solution |
|-------|----------|
| CDK deploys to wrong region | Pass explicit `--context awsRegion=...` flags |
| `Vpc.fromLookup()` fails with Tokens | Use `Vpc.fromVpcAttributes()` instead |
| Python imports fail in Docker | Use relative imports (`.module`) not absolute |
| Docker `pip install .` fails | Copy src directory first (src-layout packages) |
| Secrets not available in workflow | Explicitly reference: `${{ secrets.SECRET_NAME }}` |
| OIDC auth fails | Add job-level `permissions: {id-token: write, contents: read}` |
| SSM put-parameter fails | Don't use `--tags` with `--overwrite` |
| Angular tests fail (HTTP) | Add `provideHttpClient()` and `provideHttpClientTesting()` |
| Test script requires AWS config | Set minimal vars directly, don't source `load-env.sh` |
| Cache save "fails" | Normal behavior when dependencies unchanged |

---

## Cost Optimization Notes
- **Main costs**: NAT Gateways (~$32/mo/AZ), ALB (~$16/mo), Fargate, ECR storage
- **Actions**: Budget alerts, resource tagging, review AZ count for dev/staging
- **Consider**: VPC endpoints to reduce NAT Gateway traffic, ECR lifecycle policies

---

## Deprecation Warnings (Non-Blocking)
1. `pyproject.toml`: `license = {text = "MIT"}` → `license = "MIT"` (Deadline: 2026-Feb-18)
2. Frontend: `S3Origin` → `S3BucketOrigin` from `@aws-cdk-lib/aws-cloudfront-origins`
3. App API: `pointInTimeRecovery` → `pointInTimeRecoverySpecification`
4. App API: `containerInsights` → `containerInsightsV2`

---

## Pre-Flight Checklist (Each Phase)
- [ ] Review lessons from previous phases
- [ ] Verify all GitHub Secrets/Variables configured
- [ ] Check for dependency updates and breaking changes
- [ ] Create composite actions for repeated patterns
- [ ] Test scripts locally before committing
- [ ] Use explicit context flags in CDK commands
- [ ] Implement proper error handling with meaningful messages
- [ ] Add SSM parameters for cross-stack references
- [ ] Test deployment in dev environment first
- [ ] Verify stack names follow naming convention
- [ ] Commit all lock files

---

## Key Takeaways

1. **Defensive Programming**: Anticipate failures, provide detailed errors, make config explicit
2. **Separation of Concerns**: Infrastructure stack separate from application stacks
3. **Configuration Injection**: Environment variables > context files for flexibility
4. **Script Portability**: All logic in bash scripts, not GitHub Actions YAML
5. **Cross-Stack via SSM**: Never hardcode resource references
6. **CDK Explicit Context**: Always pass region/account as context flags
7. **Python Relative Imports**: Always use `.module` for sibling imports in packages
8. **Docker Source Truth**: `pyproject.toml` for dependencies, copy src before install
9. **Composite Actions**: Abstract common patterns early (60% code reduction)
10. **Document During Development**: Not retroactively
