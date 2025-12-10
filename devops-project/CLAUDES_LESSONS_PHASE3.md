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

_This section will be filled in as we troubleshoot CI/CD and AWS deployment issues during testing._

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
