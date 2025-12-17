# Claude's Lessons Learned - Phase 5: AgentCore Gateway & MCP Stack

## Phase Overview
Phase 5 implements the AWS Bedrock AgentCore Gateway with Lambda-based MCP tools for research and analysis capabilities.

---

## Technical Lessons

### AWS CDK Infrastructure
_(To be documented during testing)_

### Lambda Functions & MCP Tools
_(To be documented during testing)_

### Gateway & Gateway Targets
_(To be documented during testing)_

### IAM & Security
_(To be documented during testing)_

### CI/CD Pipeline
_(To be documented during testing)_

### CDK CLI Availability in GitHub Actions Jobs

**Problem**: Jobs that run CDK commands (`cdk synth`, `cdk diff`, `cdk deploy`) fail with "cdk: command not found" even when node_modules cache is restored.

**Root Cause**: 
- Caching `node_modules` preserves installed packages but doesn't add CDK CLI to the PATH
- Each GitHub Actions job runs in a fresh VM with no global npm packages
- The CDK CLI (`npx cdk` or `cdk`) requires proper npm environment setup

**Solution Pattern**:
Every job that executes CDK commands must include an "Install system dependencies" step BEFORE running the CDK script:

```yaml
- name: Install system dependencies
  run: |
    bash scripts/common/install-deps.sh

- name: <CDK Operation>
  run: |
    bash scripts/stack-<name>/<operation>.sh
```

**Jobs Requiring This Pattern**:
- `synth-cdk` - Runs `cdk synth`
- `test-cdk` - Runs `cdk diff`  
- `deploy-stack` - Runs `cdk deploy`

**Why Cache Alone Isn't Enough**:
- `actions/cache@v4` restores file contents but doesn't configure the npm environment
- `install-deps.sh` sets up Node.js, npm, and ensures CDK CLI is accessible via PATH
- This matches the pattern used successfully in Infrastructure Stack workflow

**Alternative Considered**: Adding `npm install -g aws-cdk` to individual scripts was rejected because:
- Violates "scripts should be portable" principle
- Creates inconsistency across stacks
- `install-deps.sh` already handles all system-level dependencies centrally

---

## Gotchas & Workarounds

### CDK Diff Cannot Catch Service-Level Validation Errors

**Problem**: `cdk diff` in test-cdk job successfully passes, but deployment fails with validation errors like "HYBRID is not a valid enum value" or "ERROR is not a valid enum value".

**Root Cause**: 
- `cdk diff` only validates CDK synthesis and CloudFormation template structure
- Service-specific constraints (enum values, property combinations, etc.) are only validated when CloudFormation attempts to create a change set during actual deployment
- The message "Could not create a change set, will base the diff on template differences" means CDK fell back to template-only comparison without AWS service validation

**What test-cdk CAN catch**:
- TypeScript compilation errors
- Missing required CDK properties
- CloudFormation template syntax errors
- Type mismatches in CDK constructs

**What test-cdk CANNOT catch**:
- Invalid enum values for AWS service properties (e.g., `exceptionLevel: 'ERROR'` when only `'DEBUG'` is supported)
- Service-specific validation rules
- AWS service limits
- Runtime resource conflicts

**Solution**: Accept this as a CDK limitation. Service-level validation only happens at deploy time. Best practices:
1. Carefully read AWS CDK documentation for service-specific properties
2. Check CloudFormation/CDK docs for valid enum values before implementing
3. Test deployments in dev environment first
4. When deployment fails with validation errors, fix and redeploy

**Alternative Considered**: Using `cdk deploy --no-execute` in test job to create actual change sets - rejected because:
- Requires AWS credentials in test job (security concern)
- Slower than template-only diff
- Creates/deletes change sets unnecessarily
- Doesn't align with other stack test patterns

---

### Missing log_success Function in Scripts

**Problem**: Gateway stack scripts failed with "log_success: command not found" error during execution.

**Root Cause**: 
- `log_success` function is not defined in `scripts/common/load-env.sh`
- Each stack's scripts define their own logging functions locally
- Gateway scripts were initially created without this local function definition

**Solution**: Add `log_success` function definition to each script after sourcing `load-env.sh`:

```bash
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}
```

**Scripts Requiring This Fix**:
- All gateway stack scripts: `install.sh`, `build-cdk.sh`, `synth.sh`, `deploy.sh`, `test.sh`, `test-cdk.sh`

**Pattern Observation**: This pattern is consistent across all other stacks (Infrastructure, App API, Inference API, Frontend) - each defines `log_success` locally rather than centralizing in `load-env.sh`.

---

### Issue 1: [Placeholder]
_(To be documented when issues are encountered)_

---

## Best Practices Reinforced

### CDK Stack Naming: Logical IDs vs Prefixed Names

**Issue**: Initial gateway scripts built stack names with `${CDK_PROJECT_PREFIX}-GatewayStack` when calling CDK commands, causing "No stacks match the name" errors.

**Root Cause**: Inconsistency with how other stacks reference themselves in scripts.

**Correct Pattern**:
- **In infrastructure.ts**: Use `stackName` property with prefix for CloudFormation stack names:
  ```typescript
  new GatewayStack(app, 'GatewayStack', {
    stackName: `${config.projectPrefix}-GatewayStack`,  // CloudFormation name
  });
  ```

- **In scripts (synth/deploy)**: Use the logical construct ID, NOT the prefixed stackName:
  ```bash
  # ✅ CORRECT - use logical ID
  cdk synth GatewayStack ${CONTEXT_PARAMS}
  cdk deploy GatewayStack --app "cdk.out/"
  
  # ❌ WRONG - don't build name with prefix
  STACK_NAME="${CDK_PROJECT_PREFIX}-GatewayStack"
  cdk synth "${STACK_NAME}" ${CONTEXT_PARAMS}
  ```

**Why This Matters**:
- CDK CLI resolves stacks by their **logical construct ID** (first parameter to constructor)
- The `stackName` property only affects the **CloudFormation stack name** in AWS
- Trying to reference stacks by their CloudFormation name fails CDK commands
- All existing stacks (Infrastructure, AppApi, InferenceApi, Frontend) follow this pattern

**Reference Examples**:
- `scripts/stack-app-api/synth.sh`: `cdk synth AppApiStack`
- `scripts/stack-inference-api/deploy.sh`: `cdk deploy InferenceApiStack`
- `scripts/stack-infrastructure/synth.sh`: `cdk synth InfrastructureStack`

**Consistency Check**: When adding new stacks, always grep existing scripts to match the established pattern.

---

### Pattern 1: [Placeholder]
_(To be documented during implementation)_

---

## Testing Insights

### Test 1: [Placeholder]
_(To be documented during testing phase)_

---

## Documentation & References
- Reference Architecture: [aws-samples/sample-strands-agent-with-agentcore](https://github.com/aws-samples/sample-strands-agent-with-agentcore/tree/main/agent-blueprint/agentcore-gateway-stack)
- MCP Protocol: Model Context Protocol for tool integration
- Google Custom Search API: Documentation for search tool implementation

---

**Note**: This document will be populated with actual learnings as we encounter issues during implementation and testing.
