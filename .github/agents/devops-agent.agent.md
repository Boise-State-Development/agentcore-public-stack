---
description: 'Agent to help with THE_PLAN.md'
tools: ['runCommands', 'runTasks', 'edit', 'runNotebooks', 'search', 'new', 'extensions', 'usages', 'vscodeAPI', 'problems', 'changes', 'testFailure', 'openSimpleBrowser', 'fetch', 'githubRepo', 'todos', 'runSubagent', 'runTests']
---
# System Instructions for AWS CDK Multi-Stack Deployment

## Project Architecture

**5-Stack AWS CDK System**:
1. **Infrastructure Stack** - Foundation layer (VPC, ALB, ECS Cluster)
2. **Frontend Stack** - S3 + CloudFront + Route53
3. **App API Stack** - Fargate service for application API
4. **Inference API Stack** - bedrock agentcore runtime for inference API hosting
5. **AgentCore Gateway Stacks** - Bedrock Agentcore Gateway implementation with MCP tools deployed to lambda

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
- **Logging functions**: Define `log_success()` and `log_warning()` locally in each script (after sourcing load-env.sh):
```bash
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_warning() {
    echo -e "\033[1;33m⚠ $1\033[0m"
}
```

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

### Modular Workflow Architecture (9-Job Pattern for Docker Stacks)

**Architecture Pattern**:
```
Workflow Structure:
├── install (dependencies, cache node_modules)
├── Parallel Track A: Docker Pipeline
│   ├── build-docker (generate git SHA tag, export as tar artifact)
│   ├── test-docker (download, load, test image)
│   └── push-to-ecr (download, load, push with git SHA tag)
├── Parallel Track B: CDK Pipeline  
│   ├── build-cdk (compile TypeScript)
│   ├── synth-cdk (synthesize CloudFormation, upload templates)
│   ├── test-cdk (validate with cdk diff)
│   └── deploy-infrastructure (use pre-synthesized templates)
└── test-python (unit tests, parallel to Docker track)
```

**Key Principles**:
1. **No Inline Logic**: All workflow steps call backing scripts - zero bash in YAML
2. **Standardized Naming**: Consistent step names ("Configure AWS credentials", "Upload synthesized templates")
3. **Parallel Execution**: Independent tracks (Docker, CDK, Python) run simultaneously
4. **Artifact Passing**: Share Docker images (tar) and CFN templates between jobs
5. **Single Responsibility**: Each job performs one clear function
6. **Job Outputs**: IMAGE_TAG generated once, propagated via outputs/env

**Benefits**: Clear failure points, 40-60% faster execution via parallelization, locally testable scripts

### Docker Image Sharing Between Jobs

**Critical**: GitHub Actions jobs are isolated - Docker images don't persist between jobs.

**Solution**: Export/import images as tar artifacts:

1. **Build Job** - Generate tag once, export image:
```yaml
build-docker:
  outputs:
    image-tag: ${{ steps.set-tag.outputs.IMAGE_TAG }}
  steps:
    - name: Set image tag
      id: set-tag
      run: |
        IMAGE_TAG=$(git rev-parse --short HEAD)
        echo "IMAGE_TAG=${IMAGE_TAG}" >> $GITHUB_OUTPUT
    
    - name: Build and export Docker image
      uses: docker/build-push-action@v6
      with:
        context: .
        file: backend/Dockerfile.app-api
        tags: ${{ env.CDK_PROJECT_PREFIX }}-app-api:${{ steps.set-tag.outputs.IMAGE_TAG }}
        outputs: type=docker,dest=${{ runner.temp }}/app-api-image.tar
    
    - name: Upload Docker image artifact
      uses: actions/upload-artifact@v4
      with:
        name: app-api-docker-image
        path: ${{ runner.temp }}/app-api-image.tar
        retention-days: 1
```

2. **Test/Push Jobs** - Download and load:
```yaml
test-docker:
  needs: build-docker
  env:
    IMAGE_TAG: ${{ needs.build-docker.outputs.image-tag }}
  steps:
    - name: Download Docker image artifact
      uses: actions/download-artifact@v4
      with:
        name: app-api-docker-image
        path: ${{ runner.temp }}
    
    - name: Load Docker image
      run: |
        docker load --input ${{ runner.temp }}/app-api-image.tar
        docker image ls -a
```

**Best Practices**:
- Use `docker/build-push-action@v6` (not shell scripts) for better caching/multi-platform support
- Generate git SHA tag once in build job, propagate via `outputs` and `env`
- Scripts accept `IMAGE_TAG` env var with fallback: `IMAGE_TAG="${IMAGE_TAG:-latest}"`
- Keep artifact retention low (1 day) to minimize storage costs
- Reference: https://docs.docker.com/build/ci/github-actions/share-image-jobs/

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

### CDK Synth/Deploy Pattern

**Best Practice**: Synthesize CloudFormation once, reuse for test and deploy.

**Required Scripts** (per stack):
- `build-cdk.sh` - Compile TypeScript CDK code
- `synth.sh` - Synthesize CloudFormation with all context parameters  
- `test-cdk.sh` - Validate using `cdk diff --app "cdk.out/"`
- `deploy.sh` - Deploy with pre-synthesized template detection

**deploy.sh Pattern**:
```bash
if [ -d "cdk.out" ] && [ -f "cdk.out/AppApiStack.template.json" ]; then
    log_info "Using pre-synthesized templates from cdk.out/"
    cdk deploy AppApiStack --app "cdk.out/" --require-approval never
else
    log_info "Synthesizing on-the-fly"
    cdk deploy AppApiStack [all context parameters] --require-approval never
fi
```

**Critical**: Context parameters in synth.sh and deploy.sh must match exactly.

**Stack Naming Consistency**:
```bash
# ✅ CORRECT - Use logical construct ID in scripts
cdk synth GatewayStack ${CONTEXT_PARAMS}
cdk deploy GatewayStack --app "cdk.out/"

# ❌ WRONG - Don't build name with prefix
STACK_NAME="${CDK_PROJECT_PREFIX}-GatewayStack"
cdk synth "${STACK_NAME}" ${CONTEXT_PARAMS}
```
**Why**: CDK CLI resolves stacks by logical ID (constructor param), not CloudFormation `stackName` property.

**CDK Bootstrap Rule**: Always run from project root (not CDK app directory):
```bash
# Bad - loads CDK app, triggers config.ts, may use wrong context
cd infrastructure
cdk bootstrap aws://${ACCOUNT}/${REGION}

# Good - neutral directory, no app loading
cd project-root  
cdk bootstrap aws://${ACCOUNT}/${REGION}  # No --context flags!
```

### Composite Actions
Create reusable patterns at `.github/actions/<action-name>/action.yml`:
- `configure-aws-credentials` (OIDC → Access Keys fallback)
- Potential: `setup-cdk-environment`, `build-and-push-docker`, `deploy-cdk-stack`

**Benefits**: 60% code reduction, single source of truth, easier updates

### Caching Behavior
- Cache keys based on `package-lock.json` hash
- "Failed to save cache" = **normal** when dependencies unchanged (reusing existing cache)
- Ensure install scripts create what's being cached (e.g., CDK dependencies)

### CDK CLI Availability
**Critical**: Jobs running CDK commands require explicit dependency installation:
```yaml
# Jobs that run cdk synth, cdk diff, or cdk deploy
- name: Install system dependencies
  run: bash scripts/common/install-deps.sh

- name: Run CDK command
  run: bash scripts/stack-<name>/<operation>.sh
```
**Why**: Caching `node_modules` preserves files but doesn't configure npm environment or add CDK to PATH. Each job runs in fresh VM requiring setup.

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

### Gateway Stack (Lambda + AgentCore)
- **Service**: AWS Bedrock AgentCore Gateway with Lambda-based MCP tools
- **CLI**: Use `aws bedrock-agentcore-control` (NOT `bedrock-agentcore`)
- **Response parsing**: Gateway targets in `.items[]` array
- **Stack reference**: Use logical ID `GatewayStack` in scripts, not `${CDK_PROJECT_PREFIX}-GatewayStack`
- **Testing**: Remove validation from deploy.sh, handle in test.sh separately

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

### AWS Bedrock AgentCore CLI
**Critical**: Use correct service name for AgentCore operations:

```bash
# ✅ CORRECT - bedrock-agentcore-control
aws bedrock-agentcore-control get-gateway \
    --gateway-identifier "${GATEWAY_ID}" \
    --region "${CDK_AWS_REGION}"

aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "${GATEWAY_ID}" \
    --region "${CDK_AWS_REGION}"

# ❌ WRONG - bedrock-agentcore (runtime service, different API)
aws bedrock-agentcore get-gateway  # Will fail with "invalid choice"
```

**Response Format**: Gateway target listings return `items[]` array, not `gatewayTargetSummaries[]`:
```bash
# Parse response correctly
TARGET_COUNT=$(echo "${TARGETS}" | jq '.items | length')
echo "${TARGETS}" | jq -r '.items[] | "\(.name): \(.description)"'
```

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
### CDK Test Limitations
**Important**: `cdk diff` has limited validation scope:
- ✅ **Can detect**: TypeScript errors, missing CDK properties, template syntax errors
- ❌ **Cannot detect**: Invalid enum values, service-specific validation, AWS limits
- Service-level validation only happens during actual deployment
- Message "Could not create a change set" = template-only diff (no AWS service validation)

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
| CDK bootstrap loads app context | Run from project root, NOT infrastructure directory, NO `--context` flags |
| `Vpc.fromLookup()` fails with Tokens | Use `Vpc.fromVpcAttributes()` instead |
| Docker image not available in next job | Export as tar artifact, upload/download between jobs |
| Image tag mismatch between jobs | Generate git SHA once in build, propagate via job `outputs` |
| Python imports fail in Docker | Use relative imports (`.module`) not absolute |
| Docker `pip install .` fails | Copy src directory first (src-layout packages) |
| Secrets not available in workflow | Explicitly reference: `${{ secrets.SECRET_NAME }}` |
| OIDC auth fails | Add job-level `permissions: {id-token: write, contents: read}` |
| SSM put-parameter fails | Don't use `--tags` with `--overwrite` |
| Angular tests fail (HTTP) | Add `provideHttpClient()` and `provideHttpClientTesting()` |
| Test script requires AWS config | Set minimal vars directly, don't source `load-env.sh` |
| Cache save "fails" | Normal behavior when dependencies unchanged |
| Synth/deploy context mismatch | Ensure identical parameters in synth.sh and deploy.sh |
| Deployment slower than expected | Check if using pre-synthesized templates (cdk.out/), enable parallelization |
| `cdk diff` passes but deploy fails validation | CDK diff can't catch service-level enum/constraint errors; only happens at deploy |
| AWS CLI "invalid choice" for AgentCore | Use `bedrock-agentcore-control` (control plane), not `bedrock-agentcore` (runtime) |
| Gateway targets parsing returns 0 items | Response uses `.items[]` array, not `.gatewayTargetSummaries[]` |
| CDK "No stacks match" error | Use logical ID (`GatewayStack`), not prefixed name (`${PREFIX}-GatewayStack`) |
| `log_success: command not found` | Define logging functions locally in script after sourcing load-env.sh |
| CDK commands fail in GitHub Actions job | Add "Install system dependencies" step before running CDK scripts |

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

## Key Takeaways

1. **Defensive Programming**: Anticipate failures, provide detailed errors, make config explicit
2. **Separation of Concerns**: Infrastructure stack separate from application stacks
3. **Configuration Injection**: Environment variables > context files for flexibility
4. **Script Portability**: All logic in bash scripts, not GitHub Actions YAML
5. **Cross-Stack via SSM**: Never hardcode resource references
6. **CDK Bootstrap Isolation**: Run from project root, no context parameters, avoid app loading
7. **Job Isolation**: Docker images/artifacts don't persist - use tar export/import pattern
8. **Single Source of Truth**: Generate version tags (git SHA) once, propagate via job outputs
9. **Modular Workflows**: 9-job architecture with parallel Docker/CDK tracks (40-60% faster)
10. **Pre-Synthesized Templates**: Synthesize once, reuse for test and deploy
11. **Context Parameter Discipline**: synth.sh and deploy.sh must have identical parameters
12. **Python Relative Imports**: Always use `.module` for sibling imports in packages
13. **Docker Source Truth**: `pyproject.toml` for dependencies, copy src before install
14. **Composite Actions**: Abstract common patterns early (60% code reduction)
15. **Document During Development**: Not retroactively

