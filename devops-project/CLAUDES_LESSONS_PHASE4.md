# Phase 4: Lessons Learned - AWS Bedrock AgentCore Runtime Refactoring

## Overview
This document captures lessons learned during Phase 4 implementation: Refactoring the Inference API Stack from ECS/Fargate to AWS Bedrock AgentCore Runtime.

---

## Technical Challenges

### Challenge 1: CDK Bootstrap Loading App Context

**Issue:**
Running `cdk bootstrap` from within a CDK app directory (containing cdk.json) causes it to load the entire CDK application and its configuration, triggering config.ts logging and potentially using incorrect context values.

**Root Cause:**
CDK CLI automatically searches for and loads the CDK app in the current directory when running any command, including bootstrap. This causes the configuration loading logic to execute even though bootstrap doesn't need app-specific context - it only needs AWS account and region.

**Solution:**
Always run `cdk bootstrap` from a neutral directory (without cdk.json) to avoid loading the CDK app:
```bash
# Bad - runs from CDK app directory
cd infrastructure
cdk bootstrap aws://${ACCOUNT}/${REGION}

# Good - runs from parent directory
cd project-root
cdk bootstrap aws://${ACCOUNT}/${REGION}
```

**Lesson Learned:**
- `cdk bootstrap` should never be passed `--context` parameters (they trigger app loading)
- Run `cdk bootstrap` from outside the CDK app directory to prevent unwanted app initialization
- Only `cdk synth` and `cdk deploy` should load the app and receive context parameters
- This prevents unexpected side effects like logging, incorrect context values, or bootstrap failures

---

### Challenge 2: GitHub Actions Job Isolation and Docker Image Sharing

**Issue:**
Initial workflow implementations attempted to build Docker images in one job and test/push them in subsequent jobs. Since each GitHub Actions job runs in an isolated environment, the built Docker image was not available in subsequent jobs, causing test-docker and push-to-ecr jobs to fail.

**Root Cause:**
GitHub Actions jobs are completely isolated - they run on separate runners with separate filesystems. Docker images built in one job do not persist to subsequent jobs unless explicitly shared through artifacts or a registry. This is by design for clean, reproducible builds.

**Solution:**
Implement Docker image sharing using the official Docker GitHub Actions pattern:

1. **Build Job** - Export image as tar artifact:
```yaml
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

2. **Test/Push Jobs** - Download and load image:
```yaml
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

**Lesson Learned:**
- Never assume Docker images persist between GitHub Actions jobs
- Use `docker/build-push-action@v6` with `outputs: type=docker,dest=` to export images as tar files
- Upload/download tar files as artifacts for sharing between jobs
- Keep artifact retention-days low (1 day) for Docker images to minimize storage costs
- Always verify loaded images with `docker image ls -a` for debugging
- Reference: https://docs.docker.com/build/ci/github-actions/share-image-jobs/

---

### Challenge 3: Docker Image Tagging Strategy and Environment Variable Propagation

**Issue:**
Initial implementation hardcoded `:latest` tag in build and test scripts. This caused version tracking issues and didn't align with the existing git SHA-based tagging strategy used in ECR push scripts.

**Root Cause:**
Multiple disconnected assumptions:
- Build job tagged images as `:latest`
- Test script hardcoded `${PROJECT_PREFIX}-app-api:latest`
- Push script generated new tag using `git rev-parse --short HEAD`
- No coordination between jobs about which tag to use

**Solution:**
Centralized tag generation with job outputs and environment variables:

1. **Generate tag once in build job**:
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
        echo "Building with tag: ${IMAGE_TAG}"
    
    - name: Build and export Docker image
      uses: docker/build-push-action@v6
      with:
        tags: ${{ env.CDK_PROJECT_PREFIX }}-app-api:${{ steps.set-tag.outputs.IMAGE_TAG }}
```

2. **Pass tag to dependent jobs**:
```yaml
test-docker:
  needs: build-docker
  env:
    IMAGE_TAG: ${{ needs.build-docker.outputs.image-tag }}

push-to-ecr:
  needs: [build-docker, test-docker, test-python]
  outputs:
    image-tag: ${{ needs.build-docker.outputs.image-tag }}
  env:
    IMAGE_TAG: ${{ needs.build-docker.outputs.image-tag }}
```

3. **Update scripts to use environment variable**:
```bash
# In test-docker.sh
IMAGE_TAG="${IMAGE_TAG:-latest}"  # Fallback to latest for local testing
IMAGE_NAME="${CDK_PROJECT_PREFIX}-app-api:${IMAGE_TAG}"
```

**Lesson Learned:**
- Generate version tags once at the beginning of the workflow
- Use job `outputs` to pass values to dependent jobs
- Use job-level `env` to make outputs available to all steps in a job
- Scripts should accept environment variables with sensible defaults for local testing
- Maintain single source of truth for version tagging across entire workflow
- Git SHA provides immutable, traceable version identifiers

---

## Configuration Issues

### Issue 1: [To be filled during testing/troubleshooting]

**Problem:**
[Description]

**Resolution:**
[How it was fixed]

**Best Practice:**
[What to do in the future]

---

## AWS-Specific Learnings

### Learning 1: [To be filled during testing/troubleshooting]

**Topic:**
[Subject area]

**Discovery:**
[What was learned]

**Application:**
[How to apply this knowledge]

---

## Build & Deployment Insights

### Insight 1: Modular GitHub Actions Workflow Architecture

**Observation:**
Migrated from monolithic workflows with inline logic to modular, job-based architecture with backing scripts for all operations. Applied systematic refactoring to frontend and app-api workflows following consistent patterns.

**Architecture Pattern:**
```
Workflow Structure for Stack Deployment:
├── install (base dependencies, cache node_modules)
├── Parallel Track A: Docker Pipeline
│   ├── build-docker (export as tar artifact)
│   ├── test-docker (download, load, test)
│   └── push-to-ecr (download, load, push)
├── Parallel Track B: CDK Pipeline
│   ├── build-cdk (compile TypeScript)
│   ├── synth-cdk (synthesize CloudFormation, upload artifacts)
│   ├── test-cdk (validate templates using cdk diff)
│   └── deploy-infrastructure (use pre-synthesized templates)
└── test-python (unit tests, parallel to Docker track)
```

**Backing Scripts Created:**
- `scripts/stack-{name}/build-cdk.sh` - Compile CDK TypeScript to JavaScript
- `scripts/stack-{name}/synth.sh` - Synthesize CloudFormation with all context parameters
- `scripts/stack-{name}/test-cdk.sh` - Validate templates using `cdk diff --app "cdk.out/"`
- `scripts/stack-{name}/deploy.sh` - Deploy with pre-synthesized template detection

**Key Principles:**
1. **No Inline Logic**: All workflow steps call backing scripts - zero bash logic in YAML
2. **Standardized Naming**: Consistent step names across all workflows:
   - "Configure AWS credentials" (lowercase, consistent format)
   - "Save X cache" / "Restore X cache"
   - "Upload synthesized templates" / "Download synthesized templates"
   - "Build CDK" / "Synthesize CloudFormation template" / "Validate CloudFormation template"
3. **Parallel Execution**: Independent tracks (Docker, CDK, Python tests) run simultaneously
4. **Artifact Passing**: CDK templates and Docker images shared via artifacts, not rebuilt
5. **Single Responsibility**: Each job performs one clear function

**Context Parameter Consistency:**
All context parameters must match exactly between synth.sh and deploy.sh:
```bash
# synth.sh and deploy.sh use identical parameters
cdk synth/deploy AppApiStack \
    --context environment="${DEPLOY_ENVIRONMENT}" \
    --context projectPrefix="${CDK_PROJECT_PREFIX}" \
    --context awsAccount="${CDK_AWS_ACCOUNT}" \
    --context awsRegion="${CDK_AWS_REGION}" \
    --context vpcCidr="${CDK_VPC_CIDR}" \
    --context infrastructureHostedZoneDomain="${CDK_HOSTED_ZONE_DOMAIN}" \
    --context appApi.enabled="${CDK_APP_API_ENABLED}" \
    --context appApi.cpu="${CDK_APP_API_CPU}" \
    --context appApi.memory="${CDK_APP_API_MEMORY}" \
    --context appApi.desiredCount="${CDK_APP_API_DESIRED_COUNT}" \
    --context appApi.maxCapacity="${CDK_APP_API_MAX_CAPACITY}"
```

**Pre-Synthesized Template Detection:**
deploy.sh scripts detect and use pre-synthesized templates when available:
```bash
if [ -d "cdk.out" ] && [ -f "cdk.out/AppApiStack.template.json" ]; then
    log_info "Using pre-synthesized templates from cdk.out/"
    cdk deploy AppApiStack --app "cdk.out/" --require-approval never
else
    log_info "Synthesizing templates on-the-fly"
    cdk deploy AppApiStack [all context parameters] --require-approval never
fi
```

**AWS Credentials Placement:**
- **synth-cdk job**: Requires AWS credentials (Route53 HostedZone.fromLookup())
- **test-cdk job**: Requires AWS credentials (cdk diff needs account context)
- **push-to-ecr job**: Requires AWS credentials (ECR push)
- **deploy-infrastructure job**: Requires AWS credentials (CDK deploy)

**Implication:**
- Workflows are maintainable, testable, and professional
- Parallel execution reduces CI/CD runtime significantly
- Consistent patterns across all stacks reduce cognitive load
- Scripts can be tested locally without GitHub Actions
- Changes to deployment logic only require script updates, not workflow changes

**Action Taken:**
- Refactored frontend.yml to 8 distinct jobs with parallel tracks
- Refactored app-api.yml to 9 distinct jobs with Docker and CDK parallelization
- Standardized step names across infrastructure.yml, frontend.yml, and app-api.yml
- Created build-cdk.sh, synth.sh, test-cdk.sh for frontend and app-api stacks
- Updated all deploy.sh scripts to detect pre-synthesized templates
- Simplified bootstrap commands across all stacks to match infrastructure pattern

---

### Insight 2: Docker Build Action Configuration

**Observation:**
Using `docker/build-push-action@v6` provides significant advantages over running `docker build` commands in shell scripts within GitHub Actions.

**Benefits:**
1. **Buildx Integration**: Automatically configures BuildKit for advanced features
2. **Caching**: Built-in support for layer caching strategies (GitHub cache, registry cache)
3. **Multi-platform**: Easy configuration for building multi-architecture images
4. **Output Flexibility**: Simple syntax for exporting to different formats (docker, tar, registry)
5. **Metadata**: Automatic generation of image metadata and labels

**Configuration Pattern:**
```yaml
- name: Set up Docker Buildx
  uses: docker/setup-buildx-action@v3

- name: Build and export Docker image
  uses: docker/build-push-action@v6
  with:
    context: .                           # Build context (project root)
    file: backend/Dockerfile.app-api     # Path to Dockerfile
    tags: ${{ env.PREFIX }}-api:${{ steps.tag.outputs.TAG }}
    outputs: type=docker,dest=${{ runner.temp }}/image.tar
    # Optional: Add caching
    # cache-from: type=gha
    # cache-to: type=gha,mode=max
```

**Implication:**
- More maintainable than shell scripts with manual docker commands
- Better performance through integrated caching
- Consistent with Docker's recommended CI/CD practices
- Future-proof for multi-platform builds if needed

**Action Taken:**
- Replaced `bash scripts/stack-app-api/build.sh` with `docker/build-push-action@v6` in workflow
- Kept build.sh for local development but use action for CI/CD
- Configured proper context and file paths for Docker build
- Used `outputs: type=docker,dest=` for artifact export

---

## Testing Observations

### Observation 1: [To be filled during testing/troubleshooting]

**Test Case:**
[What was being tested]

**Result:**
[Outcome]

**Adjustment:**
[Changes made based on result]

---

## Future Recommendations

### Recommendation 1: [To be filled during testing/troubleshooting]

**Area:**
[Domain of improvement]

**Suggestion:**
[What to do]

**Rationale:**
[Why this is important]

---

## Summary

### Workflow Architecture Modernization

During Phase 4, we systematically refactored GitHub Actions workflows to establish professional, maintainable CI/CD patterns:

**Key Achievements:**
1. ✅ **Modular Job Architecture** - Separated workflows into distinct, single-responsibility jobs
2. ✅ **Parallel Execution** - Docker and CDK pipelines run simultaneously where possible
3. ✅ **Artifact-Based Sharing** - Docker images and CloudFormation templates passed via artifacts
4. ✅ **Script-Based Logic** - Zero inline logic in workflows, all operations use backing scripts
5. ✅ **Standardized Naming** - Consistent step names across all workflows
6. ✅ **Version Tracking** - Git SHA-based image tagging with centralized tag generation
7. ✅ **Pre-Synthesized Templates** - deploy.sh scripts detect and use pre-synthesized CFN templates

**Established Patterns:**

| Pattern | Implementation | Benefit |
|---------|---------------|---------|
| Job Isolation | Separate jobs for build, test, synth, deploy | Clear failure points, parallel execution |
| Backing Scripts | build-cdk.sh, synth.sh, test-cdk.sh | Testable locally, maintainable |
| Artifact Passing | Docker tar exports, CFN template uploads | Avoid rebuilds, faster deployments |
| Job Outputs | IMAGE_TAG propagated through workflow | Single source of truth for versions |
| Context Consistency | Identical parameters in synth and deploy | Prevent configuration drift |
| Bootstrap Isolation | Run from project root, no context params | Avoid app loading, clean bootstrap |

**Workflow Structure Applied To:**
- ✅ infrastructure.yml - 5 sequential jobs (baseline pattern)
- ✅ frontend.yml - 8 jobs with parallel build tracks
- ✅ app-api.yml - 9 jobs with Docker/CDK parallelization
- ✅ inference-api.yml - 9 jobs with Docker/CDK parallelization (ARM64)

**Scripts Created:**
- `scripts/stack-frontend/build-cdk.sh, synth.sh, test-cdk.sh`
- `scripts/stack-app-api/build-cdk.sh, synth.sh, test-cdk.sh`
- `scripts/stack-inference-api/build-cdk.sh, synth.sh, test-cdk.sh`

**Scripts Updated:**
- All deploy.sh scripts: Simplified bootstrap, pre-synthesized template detection
- `scripts/stack-app-api/test-docker.sh`: Dynamic IMAGE_TAG support
- `scripts/stack-inference-api/test-docker.sh`: Dynamic IMAGE_TAG support

**Total Issues Resolved:** 3 major (bootstrap context, job isolation, tagging strategy)
**Major Learnings:** 7 architectural patterns documented
**Time Spent on Phase:** Multiple sessions over Phase 4
**Phase Status:** Workflow architecture complete, all stacks refactored

### Critical Takeaways

1. **GitHub Actions Job Isolation**: Never assume state persists between jobs. Use artifacts for Docker images (tar export), synthesized templates, and other build outputs.

2. **CDK Bootstrap**: Always run from neutral directory without context parameters to prevent unexpected app loading and configuration issues.

3. **Tagging Strategy**: Generate version identifiers (git SHA) once in build job, propagate via outputs/env vars to all dependent jobs for consistency.

4. **Modular Workflows**: Separate concerns into distinct jobs with backing scripts. This enables parallel execution, clearer debugging, and maintainable pipelines.

5. **Context Parameter Discipline**: Maintain exact parameter parity between synth.sh and deploy.sh to prevent subtle deployment issues.

6. **Pre-Synthesized Templates**: Synthesize once in dedicated job, reuse in test and deploy for speed and consistency.

**Next Steps:**
- Test all workflows in live GitHub Actions environment
- Monitor parallel execution performance improvements
- Consider adding caching strategies for Docker layer caching
- Document any additional issues discovered during live testing

---

*Note: This document should be updated throughout Phase 4 implementation, especially during testing and troubleshooting activities.*
