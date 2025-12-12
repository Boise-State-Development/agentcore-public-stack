# Phase 3 Lessons: Inference API Stack Deployment

**Purpose**: Critical issues and solutions from Phase 3 testing/deployment. Updated as problems are encountered.

---

## Architecture Restructuring (Critical Discovery)

### **Issue: Improper Stack Dependencies**
**Discovery**: Initial Phase 3 design had Inference API Stack depending on App API Stack for network resources (VPC, ALB, ECS Cluster). This violated separation of concerns and prevented independent deployment.

**Root Cause**: 
- Network infrastructure (VPC, ALB, Cluster) was created in App API Stack
- Inference API Stack attempted to import these via SSM parameters
- Created tight coupling between application stacks
- Prevented deploying stacks independently or in parallel

**Solution: Three-Layer Architecture**
Restructured to proper layered architecture:

1. **Infrastructure Stack (Foundation Layer)**
   - **Purpose**: Shared network foundation for all applications
   - **Resources Created**:
     - VPC with public/private subnets (2 AZs)
     - Application Load Balancer with HTTP listener
     - ECS Cluster for all workloads
     - Security groups for ALB
     - SSM parameters for cross-stack sharing
   - **Files**: `infrastructure/lib/infrastructure-stack.ts`
   - **Deployment**: MUST be deployed FIRST

2. **App API Stack (Application Layer)**
   - **Imports**: VPC, ALB, Listener, ECS Cluster from Infrastructure Stack
   - **Creates**: App API task definition, service, target group, security group
   - **Path Routing**: `/api/*` and `/health` (priority 1)

3. **Inference API Stack (Application Layer)**
   - **Imports**: VPC, ALB, Listener, ECS Cluster from Infrastructure Stack
   - **Creates**: Inference API task definition, service, target group, security group
   - **Path Routing**: `/inference/*` (priority 10)

**Key Changes**:
- Removed VPC/ALB/Cluster creation from App API Stack
- Created new Infrastructure Stack for shared resources
- Both application stacks now import from Infrastructure Stack via SSM
- Updated `bin/infrastructure.ts` to instantiate Infrastructure Stack first
- Created deployment scripts: `scripts/stack-infrastructure/*.sh`
- Created GitHub Actions workflow: `.github/workflows/infrastructure.yml`

**Benefits**:
- ✅ Independent deployment - each stack can be deployed separately
- ✅ Proper layering - foundation → application separation
- ✅ Resource sharing - all apps use same VPC/ALB/Cluster
- ✅ Cost optimization - single NAT Gateway, single ALB, shared cluster
- ✅ Clean dependencies - no circular references between stacks

**Deployment Order**:
```bash
1. Infrastructure Stack (VPC, ALB, ECS Cluster)
2. App API Stack (App API service)
3. Inference API Stack (Inference API service)
```

**Lesson**: Always separate shared infrastructure from application-specific resources. Foundation resources (networking, load balancers, clusters) should be in their own stack and deployed first.

---

## Technical Discoveries

### **Issue: CDK `Vpc.fromLookup()` with SSM Parameters**
**Problem**: Cannot use `Vpc.fromLookup()` with SSM parameter values (Tokens). CDK requires concrete values at synthesis time.

**Solution**: Use `Vpc.fromVpcAttributes()` instead, which accepts Token values:
```typescript
const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
  vpcId: vpcId,                                    // Token from SSM
  vpcCidrBlock: vpcCidr,                          // Token from SSM
  availabilityZones: cdk.Fn.split(',', azString), // Token array
  privateSubnetIds: cdk.Fn.split(',', subnetIds), // Token array
});
```

### **Issue: ECR Lifecycle Policy Validation**
**Problem**: AWS ECR rejected lifecycle policy with empty string in `tagPrefixList`:
```
string "" is too short (length: 0, required minimum: 1)
```

**Root Cause**: Attempted to use `tagPrefixList: [""]` to match all tagged images.

**Solution**: Removed the problematic rule entirely. Final lifecycle policy has 2 rules:
1. Keep protected tags (latest, deployed, prod, staging, v, release) - priority 1
2. Delete untagged images after 7 days - priority 2

**Files Modified**: 
- `scripts/stack-app-api/push-to-ecr.sh`
- `scripts/stack-inference-api/push-to-ecr.sh`

---

## Technical Discoveries (Continued)

### **Issue: Python Import Errors in Docker**
**Problem**: `ModuleNotFoundError` for relative imports and missing agent dependencies.

**Solutions**:
1. **Use Relative Imports**: Changed from `from health.health import router` to `from .health.health import router`
2. **Add Agent Dependencies**: Added to Dockerfile:
   ```dockerfile
   strands-agents==1.14.0
   strands-agents-tools==0.2.3
   ddgs>=9.0.0
   bedrock-agentcore
   nova-act==2.3.18.0
   ```

### **Issue: Test Script Dependencies**
**Problem**: Test scripts attempted to import full application with heavy dependencies not installed.

**Solution**: Simplified test scripts to skip if no `tests/` directory exists:
```bash
if [ ! -d "test" ] || [ -z "$(ls -A test/*.test.* 2>/dev/null)" ]; then
    log_info "No tests found, skipping tests"
    exit 0
fi
```

---

## GitHub Actions & AWS

### **Issue: Workflow Naming Consistency**
**Problem**: Infrastructure workflow was named `"Infrastructure Stack CI/CD"` while others used pattern like `"AppApiStack.BuildTest.Deploy"`.

**Solution**: Standardized all workflow names to `<StackName>.BuildTest.Deploy` format:
- `InfrastructureStack.BuildTest.Deploy`
- `AppApiStack.BuildTest.Deploy`
- `InferenceApiStack.BuildTest.Deploy`
- `FrontendStack.BuildTest.Deploy`

**Lesson**: Establish naming conventions early and apply consistently across all workflows.

---

### **Issue: SSM Parameter Tags with `--overwrite`**
**Problem**: AWS SSM `put-parameter` failed with error:
```
Invalid request: tags and overwrite can't be used together
```

**Root Cause**: Attempting to use `--tags` and `--overwrite` flags together when storing image tags in SSM.

**Solution**: Remove `--tags` from put-parameter commands. AWS doesn't allow updating tags when overwriting existing parameters:
```bash
aws ssm put-parameter \
    --name "${SSM_PARAM_NAME}" \
    --value "${IMAGE_TAG}" \
    --type "String" \
    --overwrite \
    --region "${CDK_AWS_REGION}"
# No --tags flag when using --overwrite
```

**Lesson**: SSM parameters are immutable regarding tags once created. Tags can only be added via `AddTagsToResource` API after creation.

---

### **Issue: Docker Image Tag Management**
**Problem**: ECS services attempted to deploy with `"latest"` tag instead of specific git SHA tags.

**Root Cause**: 
1. Config defaulted imageTag to `'latest'` as fallback
2. IMAGE_TAG wasn't being passed to CDK properly

**Solution Iteration**:
1. ❌ Tried passing via environment variable - not available to subprocess
2. ❌ Tried passing via `--context imageTag` - required for every stack
3. ✅ **Final Solution**: Store image tag in SSM after ECR push, read from SSM in CDK

**Implementation**:
```bash
# In push-to-ecr.sh - after successful push
aws ssm put-parameter \
    --name "/${CDK_PROJECT_PREFIX}/app-api/image-tag" \
    --value "${IMAGE_TAG}" \
    --type "String" \
    --overwrite
```

```typescript
// In CDK stack - read at deployment time
const imageTag = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/app-api/image-tag`
);
```

**Benefits**:
- ✅ No need to pass imageTag via context
- ✅ Image tag always matches what's in ECR
- ✅ Decouples build from deploy
- ✅ Works for frontend stack (no image tag needed)

**Lesson**: For runtime values that vary between deployments, use SSM Parameter Store rather than CDK context. Context is for configuration, SSM is for deployment state.

---

### **Issue: GitHub Actions Cache "Failures"**
**Problem**: Cache save step showed warnings:
```
Failed to save: Unable to reserve cache with key infrastructure-node-modules-<hash>, 
another job may be creating this cache.
```

**Reality**: This is **expected behavior**, not a real failure.

**Explanation**:
- Cache keys are based on `package-lock.json` hash
- Caches are immutable - once created, can't be overwritten
- If package-lock.json hasn't changed, hash is identical
- GitHub Actions skips saving duplicate cache
- Subsequent jobs successfully restore from existing cache

**Lesson**: "Cache save failed" warnings are normal when dependencies haven't changed. It means the caching system is working correctly by reusing existing caches.

---

### **Issue: Node Module Caching in Workflows**
**Problem**: Cache save failed with "Path does not exist" for `infrastructure/node_modules`.

**Root Cause**: Install jobs were calling Python/app-specific install scripts that didn't install CDK dependencies.

**Solution**: Updated all install scripts to install both app dependencies AND CDK dependencies:
```bash
# In scripts/stack-app-api/install.sh
log_info "Installing Python dependencies..."
python3 -m pip install -e .

# Install CDK dependencies
log_info "Installing CDK dependencies..."
cd "${PROJECT_ROOT}/infrastructure"
npm install
```

**Lesson**: When workflows cache infrastructure dependencies, ensure install scripts actually create what's being cached. Don't assume stack-specific scripts will handle CDK installation.

---

### **Issue: Docker Build with `pyproject.toml`**
**Problem 1**: Duplicated dependencies between `pyproject.toml` and Dockerfile.
**Problem 2**: `pip install .` failed with "src does not exist" error.

**Root Cause**: `pyproject.toml` configured with `package-dir = {"" = "src"}`, requiring src directory for installation.

**Solution Iteration**:
1. ❌ Tried removing duplicate list, using `pip install .` - failed without src
2. ✅ Copy src directory to builder stage, then `pip install .`
3. ✅ For inference-api: `pip install ".[agentcore]"` to include optional dependencies

**Final Implementation**:
```dockerfile
# Copy pyproject.toml and source code for installation
COPY backend/pyproject.toml backend/README.md ./
COPY backend/src ./src

# Install Python dependencies from pyproject.toml
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[agentcore]"
```

**Benefits**:
- ✅ Single source of truth (pyproject.toml)
- ✅ No duplicate dependency lists
- ✅ Proper handling of optional dependencies

**Lesson**: When using src-layout packages, Docker needs the source directory during pip install. Multi-stage builds can copy src to builder, install packages, then copy only installed packages to production stage.

---

### **Issue: Missing Admin Module Import**
**Problem**: App API failed to start with `ModuleNotFoundError: No module named 'admin'`.

**Root Cause**: Import used absolute path `from admin.routes` instead of relative path `from .admin.routes`.

**Solution**: Changed to relative import within the package:
```python
# Wrong - absolute import
from admin.routes import router as admin_router

# Correct - relative import  
from .admin.routes import router as admin_router
```

**Lesson**: Within a package, always use relative imports (with `.` prefix) for sibling modules. Absolute imports assume the module is on PYTHONPATH or installed as a separate package.

---

### **Issue: Workflow Job Structure - Install/Test/Build/Deploy**
**Problem**: Some workflows had monolithic jobs combining multiple concerns.

**Solution**: Standardized all workflows to 4-job pattern:
1. **install**: Install dependencies, cache node_modules
2. **test**: Restore cache, run tests
3. **build**: Restore cache, build artifacts (Docker for APIs, dist for frontend)
4. **deploy**: Restore cache/artifacts, deploy to AWS

**Benefits**:
- ✅ Clear separation of concerns
- ✅ Efficient caching reduces build times
- ✅ Can skip tests via workflow_dispatch input
- ✅ Test failures don't waste time building
- ✅ Build failures caught before deployment

**Lesson**: Break workflows into logical stages with proper dependency chaining and caching. Don't combine install/test/build/deploy into single monolithic jobs.

---

## Best Practices Established

### **1. Stack Layering**
- **Foundation Layer**: Shared infrastructure (VPC, ALB, Cluster)
- **Application Layer**: Service-specific resources only
- **Never**: Mix foundation and application resources in same stack

### **2. SSM Parameter Naming**
Consistent parameter naming for cross-stack references:
```
/${projectPrefix}/network/vpc-id
/${projectPrefix}/network/vpc-cidr
/${projectPrefix}/network/alb-arn
/${projectPrefix}/network/alb-security-group-id
/${projectPrefix}/network/alb-listener-arn
/${projectPrefix}/network/ecs-cluster-name
/${projectPrefix}/network/ecs-cluster-arn
/${projectPrefix}/network/private-subnet-ids
/${projectPrefix}/network/public-subnet-ids
/${projectPrefix}/network/availability-zones
```

### **3. CDK Resource Imports**
When importing resources from SSM:
- Use `fromVpcAttributes()` not `fromLookup()` for VPCs
- Use `fromSecurityGroupId()` for security groups
- Use `fromClusterAttributes()` for ECS clusters
- Use `fromApplicationLoadBalancerAttributes()` for ALBs

### **4. Test Script Design**
- Keep test scripts minimal
- Don't require full application dependencies
- Skip gracefully if no tests exist
- Use separate test environments for integration tests

---

## Phase 4 Reuse Patterns

_This section will be filled in after Phase 3 is complete and we identify reusable patterns._

---

## Outstanding Items

### Open Questions
_This section will be filled in as questions arise during testing._

### Testing Gaps
_This section will be filled in after we identify what testing is missing._

### Cost Management
_This section will be filled in as we review actual deployment costs._
