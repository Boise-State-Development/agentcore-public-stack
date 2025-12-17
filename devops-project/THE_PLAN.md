# Implementation Plan: AWS CDK Multi-Stack Infrastructure & CI/CD

## Mission
Implement a 5-stack AWS CDK application with platform-agnostic CI/CD pipelines. The architecture must be fully configurable to support deployment to any AWS Region/Account by any user.

## Global Configuration Strategy
- **Config Source**: All environment-specific values (Account, Region, VPC CIDRs, Domain Names) must be injected via `cdk.context.json` or Environment Variables.
- **Naming Convention**: Resources must use a configurable `projectPrefix` to avoid naming collisions globally.
- **Secrets**: Use AWS Secrets Manager or GitHub Secrets; never commit secrets to code.

## Shell Script Inventory
All logic resides here. CI/CD pipelines merely call these scripts.

### Common Scripts
*   `scripts/common/load-env.sh` - Shared environment loader and configuration validator
*   `scripts/common/install-deps.sh` - Installs Node.js, AWS CDK, Python, Docker
*   `scripts/deploy-all.sh` - Interactive menu for local deployment orchestration

### Stack 1: Frontend Scripts
*   `scripts/stack-frontend/install.sh` - Install Angular dependencies
*   `scripts/stack-frontend/build.sh` - Build Angular application
*   `scripts/stack-frontend/test.sh` - Run frontend tests
*   `scripts/stack-frontend/deploy-cdk.sh` - Deploy CDK infrastructure
*   `scripts/stack-frontend/deploy-assets.sh` - Sync to S3 and invalidate CloudFront

### Stack 2: App API Scripts
*   `scripts/stack-app-api/install.sh` - Install Python dependencies
*   `scripts/stack-app-api/build.sh` - Build Docker image for App API
*   `scripts/stack-app-api/test.sh` - Run App API tests
*   `scripts/stack-app-api/deploy.sh` - Deploy CDK infrastructure and push image

### Stack 3: Inference API Scripts
*   `scripts/stack-inference-api/install.sh` - Install Python dependencies
*   `scripts/stack-inference-api/build.sh` - Build Docker image for Inference API
*   `scripts/stack-inference-api/test.sh` - Run Inference API tests
*   `scripts/stack-inference-api/deploy.sh` - Deploy CDK infrastructure and push image

### Stack 4: Agent Core Scripts
*   `scripts/stack-agent-core/install.sh` - Install agent dependencies
*   `scripts/stack-agent-core/test.sh` - Run agent tests
*   `scripts/stack-agent-core/deploy.sh` - Deploy CDK infrastructure

### Stack 5: Gateway Scripts
*   `scripts/stack-gateway/deploy.sh` - Deploy CDK infrastructure for Gateway/MCP

---

## Implementation Checklist

### Phase 0: Initialization & Configuration
- [x] **Create CDK Project Structure**: Initialize CDK application root directory (`infrastructure/` or `cdk/`) with `cdk init app --language=typescript`.
- [x] **Create cdk.context.json Template**: Define configurable context structure (projectPrefix, awsRegion, awsAccount, vpcCidr, domainName, etc.).
- [x] **Create Config Loader Module**: Implement `infrastructure/lib/config.ts` to read from `cdk.context.json` and validate required values.
- [x] **Create Common Scripts Directory**: Set up `scripts/common/` with `.gitkeep`.
- [x] **Script: Environment Loader**: Create `scripts/common/load-env.sh` to export configuration as environment variables.
- [x] **Script: Dependency Installer**: Create `scripts/common/install-deps.sh` to install Node.js, AWS CDK CLI, Python, pip, Docker.

**🔒 HUMAN APPROVAL REQUIRED**
- [x] [HUMAN] Phase 0 verified and approved to proceed to Phase 1

### Phase 1: Frontend Stack (Static Site)
**Goal**: S3 + CloudFront + Route53 (Optional)

#### CDK Infrastructure
- [x] **Create FrontendStack File**: Create `infrastructure/lib/frontend-stack.ts`.
- [x] **Define S3 Bucket**: Configure S3 bucket with website hosting, block public access settings, and configurable bucket name using `${projectPrefix}-frontend-${awsAccount}`.
- [x] **Define CloudFront Distribution**: Create CloudFront distribution with OAC (Origin Access Control), custom error responses, and configurable price class.
- [x] **Add CloudFront Outputs**: Export CloudFront Distribution ID and Domain Name as CloudFormation Outputs.
- [x] **Store Parameters in SSM**: Write Distribution ID and Website URL to SSM Parameter Store (`/${projectPrefix}/frontend/distribution-id` and `/${projectPrefix}/frontend/url`).
- [x] **Optional Route53 Integration**: If domain name is configured, create Route53 A record aliasing to CloudFront.

#### Build & Deploy Scripts
- [x] **Create Scripts Directory**: Set up `scripts/stack-frontend/`.
- [x] **Script: Install Dependencies**: Create `scripts/stack-frontend/install.sh` to run `npm ci` in `frontend/ai.client`.
- [x] **Script: Build Frontend**: Create `scripts/stack-frontend/build.sh` to run `ng build --configuration production`.
- [x] **Script: Test Frontend**: Create `scripts/stack-frontend/test.sh` to run `ng test --watch=false`.
- [x] **Script: Deploy CDK**: Create `scripts/stack-frontend/deploy-cdk.sh` to run `cdk deploy FrontendStack`.
- [x] **Script: Deploy Assets**: Create `scripts/stack-frontend/deploy-assets.sh` to sync `dist/` to S3 and invalidate CloudFront cache.

#### CI/CD Pipeline
- [x] **Create Workflow File**: Create `.github/workflows/frontend.yml`.
- [x] **Configure Path Triggers**: Set `paths` filter to trigger on `frontend/**` changes.
- [x] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [x] **Add Build Step**: Call `scripts/stack-frontend/install.sh` then `scripts/stack-frontend/build.sh`.
- [x] **Add Test Step**: Call `scripts/stack-frontend/test.sh`.
- [x] **Add CDK Deploy Step**: Call `scripts/stack-frontend/deploy-cdk.sh`.
- [x] **Add Asset Deploy Step**: Call `scripts/stack-frontend/deploy-assets.sh`.
- [x] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [x] [HUMAN] Phase 1 verified and approved to proceed to Phase 2

### Phase 2: App API Stack (Core Backend)
**Goal**: VPC + ALB + Fargate + RDS/DynamoDB

#### CDK Infrastructure - Networking
- [x] **Create AppApiStack File**: Create `infrastructure/lib/app-api-stack.ts`.
- [x] **Define VPC**: Create VPC with configurable CIDR, public/private subnets across 2+ AZs, NAT Gateways.
- [x] **Export VPC to SSM**: Store VPC ID in `/${projectPrefix}/network/vpc-id`.
- [x] **Export Subnets to SSM**: Store private subnet IDs in `/${projectPrefix}/network/private-subnet-ids`.
- [x] **Export Public Subnets to SSM**: Store public subnet IDs in `/${projectPrefix}/network/public-subnet-ids`.

#### CDK Infrastructure - Data Layer
- [x] **Define Security Groups**: Create security groups for ALB, ECS tasks, and RDS/DynamoDB access.
- [x] **Define Database**: Create RDS Aurora Serverless v2 OR DynamoDB table based on configuration flag.
- [x] **Export Database Connection Info**: Store connection string/endpoint in Secrets Manager and reference ARN in SSM.

#### CDK Infrastructure - Compute & Load Balancing
- [x] **Define Application Load Balancer**: Create ALB in public subnets with security group.
- [x] **Export ALB ARN to SSM**: Store ALB ARN in `/${projectPrefix}/network/alb-arn`.
- [x] **Export ALB Listener ARN**: Store listener ARN in `/${projectPrefix}/network/alb-listener-arn`.
- [x] **Define ECS Cluster**: Create Fargate cluster with configurable name.
- [x] **Define ECR Repository**: Create ECR repository for App API Docker images.
- [x] **Define Task Definition**: Create Fargate task definition with configurable CPU/memory, environment variables from config.
- [x] **Define ECS Service**: Create Fargate service with auto-scaling, health checks, and ALB target group integration.
- [x] **Add CloudFormation Outputs**: Export cluster name, service name, and task definition ARN.

#### Build & Deploy Scripts
- [x] **Create Scripts Directory**: Set up `scripts/stack-app-api/`.
- [x] **Script: Install Dependencies**: Create `scripts/stack-app-api/install.sh` to install Python dependencies via Poetry/pip.
- [x] **Script: Build Docker Image**: Create `scripts/stack-app-api/build.sh` to build and tag Docker image.
- [x] **Script: Run Tests**: Create `scripts/stack-app-api/test.sh` to run pytest.
- [x] **Script: Deploy Infrastructure**: Create `scripts/stack-app-api/deploy.sh` to deploy CDK stack and push Docker image to ECR.

#### CI/CD Pipeline
- [x] **Create Workflow File**: Create `.github/workflows/app-api.yml`.
- [x] **Configure Path Triggers**: Set `paths` filter to trigger on `backend/src/apis/app_api/**` changes.
- [x] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [x] **Add Install Step**: Call `scripts/stack-app-api/install.sh`.
- [x] **Add Build Step**: Call `scripts/stack-app-api/build.sh`.
- [x] **Add Test Step**: Call `scripts/stack-app-api/test.sh`.
- [x] **Add Deploy Step**: Call `scripts/stack-app-api/deploy.sh`.
- [x] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [x] [HUMAN] Phase 2 verified and approved to proceed to Phase 3

### Phase 3: Inference API Stack (AI Workloads)
**Goal**: Dedicated Fargate Tasks sharing Stack 2's Network

#### CDK Infrastructure
- [x] **Create InferenceApiStack File**: Create `infrastructure/lib/inference-api-stack.ts`.
- [x] **Import Network Resources**: Read VPC ID, Subnet IDs, ALB ARN, and Listener ARN from SSM Parameter Store.
- [x] **Define ECR Repository**: Create ECR repository for Inference API Docker images.
- [x] **Define Task Definition**: Create Fargate task definition with higher CPU/memory allocation for inference workloads, GPU support if configured.
- [x] **Define Target Group**: Create ALB target group with health check path `/health`.
- [x] **Define Listener Rule**: Add listener rule to ALB to route `/inference/**` to Inference API target group.
- [x] **Define ECS Service**: Create Fargate service in the imported VPC, attaching to the new target group.
- [x] **Add CloudFormation Outputs**: Export service name and task definition ARN.

#### Build & Deploy Scripts
- [x] **Create Scripts Directory**: Set up `scripts/stack-inference-api/`.
- [x] **Script: Install Dependencies**: Create `scripts/stack-inference-api/install.sh` to install Python dependencies.
- [x] **Script: Build Docker Image**: Create `scripts/stack-inference-api/build.sh` to build and tag Docker image.
- [x] **Script: Run Tests**: Create `scripts/stack-inference-api/test.sh` to run inference tests.
- [x] **Script: Deploy Infrastructure**: Create `scripts/stack-inference-api/deploy.sh` to deploy CDK stack and push Docker image to ECR.

#### CI/CD Pipeline
- [x] **Create Workflow File**: Create `.github/workflows/inference-api.yml`.
- [x] **Configure Path Triggers**: Set `paths` filter to trigger on `backend/src/apis/inference_api/**` changes.
- [x] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [x] **Add Install Step**: Call `scripts/stack-inference-api/install.sh`.
- [x] **Add Build Step**: Call `scripts/stack-inference-api/build.sh`.
- [x] **Add Test Step**: Call `scripts/stack-inference-api/test.sh`.
- [x] **Add Deploy Step**: Call `scripts/stack-inference-api/deploy.sh`.
- [x] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [x] [HUMAN] Phase 3 verified and approved to proceed to Phase 4

### Phase 4: Refactor Inference API to AWS Bedrock AgentCore Runtime
**Goal**: Refactor Phase 3's Inference API Stack to use AWS Bedrock AgentCore Runtime instead of ECS/Fargate, adding managed memory, code interpreter, and browser capabilities

#### CDK Infrastructure Refactoring
- [x] **Remove ECS/Fargate Resources**: Delete from `infrastructure/lib/inference-api-stack.ts`:
  - ECS Cluster definition
  - ECS Task Definition
  - ECS Service
  - Target Group and ALB Listener Rule (no longer needed - AgentCore Runtime has built-in HTTP endpoint)
- [x] **Add IAM Execution Role**: Create execution role for AgentCore Runtime with permissions for:
  - CloudWatch Logs (create log groups/streams, put log events)
  - X-Ray tracing (put trace segments, telemetry records)
  - CloudWatch Metrics (put metric data to `bedrock-agentcore` namespace)
  - Bedrock model access (invoke models including Claude, Nova, etc.)
  - AgentCore Gateway access (invoke gateway for MCP tools if Phase 5 is implemented)
  - AgentCore Memory access (create/retrieve events, memory records)
  - SSM Parameter Store access (read parameters under `/${projectPrefix}/`)
- [x] **Add Memory Execution Role**: Create dedicated role for AgentCore Memory with Bedrock model inference policy.
- [x] **Add AgentCore Memory**: Deploy `CfnMemory` with L1 construct including:
  - User preference extraction strategy
  - Semantic fact extraction strategy  
  - Conversation summary strategy
  - Event expiry duration (90 days for short-term memory)
- [x] **Add Code Interpreter**: Deploy `CfnCodeInterpreter` with L1 construct for Python code execution capabilities.
- [x] **Add Browser Tool**: Deploy `CfnBrowser` with L1 construct for web browsing capabilities.
- [x] **Add AgentCore Runtime**: Deploy `CfnRuntime` with L1 construct:
  - Container URI pointing to existing ECR repository (`${repositoryUri}:latest`)
  - Network mode: PUBLIC (for internet access)
  - Protocol configuration: HTTP
  - Environment variables: LOG_LEVEL, PROJECT_NAME, ENVIRONMENT, MEMORY_ARN, MEMORY_ID, BROWSER_ID, CODE_INTERPRETER_ID
  - Dependencies on execution role, memory, code interpreter, and browser resources
- [x] **Update SSM Parameter Exports**: Replace ECS-related parameters with:
  - `/${projectPrefix}/inference-api/runtime-arn`
  - `/${projectPrefix}/inference-api/runtime-id`
  - `/${projectPrefix}/inference-api/runtime-url` (HTTP endpoint for AgentCore Runtime)
  - `/${projectPrefix}/inference-api/memory-arn`
  - `/${projectPrefix}/inference-api/memory-id`
  - `/${projectPrefix}/inference-api/browser-id`
  - `/${projectPrefix}/inference-api/code-interpreter-id`
- [x] **Update CloudFormation Outputs**: Replace ECS outputs with Runtime ARN, Runtime URL, Memory ARN, ECR Repository URI.

#### Dockerfile Refactoring
- [x] **Update Dockerfile.inference-api**: Modify `backend/Dockerfile.inference-api` to support AgentCore Runtime:
  - Add FROM platform specification: `FROM --platform=linux/arm64` or build with `docker build --platform linux/arm64` (AgentCore Runtime requires ARM64)
  - Change EXPOSE from 8000 to 8080 (AgentCore Runtime expects port 8080)
  - Update CMD to use port 8080: `uvicorn apis.inference_api.main:app --host 0.0.0.0 --port 8080`
  - Update HEALTHCHECK to check port 8080 instead of 8000
  - Add OpenTelemetry instrumentation: Install `aws-opentelemetry-distro==0.10.1` and wrap CMD with `opentelemetry-instrument`
  - Set AWS region environment variables: `AWS_REGION` and `AWS_DEFAULT_REGION`
  - Create non-root user `bedrock_agentcore` with UID 1000 (security best practice)
  - Add HEALTHCHECK for `/ping` endpoint (AgentCore Runtime standard)
  - Verify all agent dependencies are included from `backend/src/agents/` (already in current Dockerfile)

#### Build & Deploy Script Updates
- [x] **Update build.sh**: Modify `scripts/stack-inference-api/build.sh`:
  - Add `--platform linux/arm64` flag to Docker build command
  - Update image tag to include git commit SHA for versioning
- [x] **Update push-to-ecr.sh**: Modify `scripts/stack-inference-api/push-to-ecr.sh`:
  - Ensure ARM64 image is pushed to ECR
  - Add tagging for both `latest` and git SHA
- [x] **Update deploy.sh**: Modify `scripts/stack-inference-api/deploy.sh`:
  - Remove ECS service update logic
  - Add AgentCore Runtime update after image push (trigger Runtime to pull new image)
  - Add validation that Runtime is healthy after deployment
- [x] **Update test-docker.sh**: Modify `scripts/stack-inference-api/test-docker.sh`:
  - Test ARM64 container locally with QEMU if on x86_64
  - Test HTTP protocol endpoints match AgentCore Runtime expectations
  - Validate health check endpoint returns 200 OK

#### Agent Integration
- [x] **Integrate Strands Agent**: Update `backend/src/apis/inference_api/main.py` to:
  - Import and initialize Strands Agent from `backend/src/agents/strands_agent/`
  - Pass Memory ARN/ID from environment variables to agent
  - Pass Code Interpreter ID and Browser ID to agent for tool access
  - Configure agent with project-specific tools from `local_tools/`
- [x] **Add Memory Integration**: Implement memory retrieval/storage in agent invocations:
  - Retrieve relevant memory events before agent execution
  - Store new memory events after agent responses
- [x] **Add Tool Integration**: Enable built-in tools in agent configuration:
  - Code interpreter tool for Python execution
  - Browser tool for web scraping and research
  - Local tools from `backend/src/agents/local_tools/`

#### CI/CD Pipeline Updates
- [x] **Update Workflow File**: Modify `.github/workflows/inference-api.yml`:
  - Update build step to use ARM64 platform flag
  - Add validation step to ensure AgentCore Runtime is accessible after deployment
  - Update test step to verify HTTP protocol compatibility
  - Keep existing path triggers for `backend/src/apis/inference_api/**`, `backend/Dockerfile.inference-api`, `infrastructure/lib/inference-api-stack.ts`
  - Add new path trigger for agent code: `backend/src/agents/**`

**🔒 HUMAN APPROVAL REQUIRED**
- [x] [HUMAN] Phase 4 verified and approved to proceed to Phase 5

### Phase 5: AgentCore Gateway & MCP Stack
**Goal**: Deploy AWS Bedrock AgentCore Gateway with Lambda-based MCP tools for research and analysis

**Reference Architecture**: Based on [aws-samples/sample-strands-agent-with-agentcore](https://github.com/aws-samples/sample-strands-agent-with-agentcore/tree/main/agent-blueprint/agentcore-gateway-stack)

**Stack Components**: Single unified stack deploying Gateway + IAM + Lambda + Gateway Targets

#### Lambda Functions (MCP Tools)
**Implementation**: Verbatim copy of google-search Lambda from reference architecture

**Purpose**: Provides two MCP tools (`google_web_search` and `google_image_search`) using Google Custom Search API

- [ ] **Create Lambda Functions Directory**: Set up `backend/lambda-functions/google-search/` for MCP tool implementation.
- [ ] **Create Lambda Handler**: Create `backend/lambda-functions/google-search/lambda_function.py` (279 lines):
  - `lambda_handler(event, context)`: Routes to tools based on `bedrockAgentCoreToolName` from context
  - `get_google_credentials()`: Retrieves credentials from Secrets Manager with global caching
  - `google_web_search(params)`: Calls Google Custom Search API, returns up to 5 results
  - `google_image_search(params)`: Calls Google Image Search API with accessibility validation, returns up to 5 images
  - `check_image_accessible(url)`: Validates image URLs via HEAD/range requests
  - `success_response(content)`: MCP-compliant response format with `content` array
  - `error_response(message)`: MCP-compliant error response
- [ ] **Create Requirements File**: Create `backend/lambda-functions/google-search/requirements.txt`:
  - `requests==2.32.4` - HTTP client for Google API calls
  - `boto3==1.35.93` - AWS SDK for Secrets Manager integration

#### CDK Infrastructure - IAM & Secrets
- [ ] **Create GatewayStack File**: Create `infrastructure/lib/gateway-stack.ts` as single unified stack.
- [ ] **Define Secrets Manager Placeholders**: Import existing secrets for Google Custom Search API:
  - `/${projectPrefix}/mcp/google-credentials` - JSON format: `{"api_key": "YOUR_KEY", "search_engine_id": "YOUR_ID"}`
  - Secret must be created manually before first deployment (see documentation section)
- [ ] **Define Lambda Execution Role**: Create role with:
  - CloudWatch Logs permissions (`logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`)
  - Secrets Manager read permissions (`secretsmanager:GetSecretValue`)
  - Managed policy: `service-role/AWSLambdaBasicExecutionRole`
- [ ] **Define Gateway Execution Role**: Create role for AgentCore Gateway with:
  - Lambda invocation permissions (`lambda:InvokeFunction`)
  - CloudWatch Logs permissions for Gateway logs
  - Service principal: `bedrock-agentcore.amazonaws.com`

#### CDK Infrastructure - Lambda Functions
- [ ] **Define Lambda Functions**: Create Lambda function using CDK `Code.fromAsset()`:
  - **Function**: `mcp-google-search`
  - Runtime: `PYTHON_3_13`
  - Architecture: `ARM_64`
  - Handler: `lambda_function.lambda_handler`
  - Code location: `Code.fromAsset('backend/lambda-functions/google-search')` (CDK handles ZIP creation and upload)
  - Environment variables:
    - `GOOGLE_CREDENTIALS_SECRET_NAME`: `${projectPrefix}/mcp/google-credentials`
    - `LOG_LEVEL`: `INFO`
  - Timeout: 60 seconds
  - Memory: 512 MB
  - Role: Lambda execution role from IAM section
- [ ] **Add Lambda Permissions**: Grant Gateway permission to invoke Lambda function via `addPermission()`.
- [ ] **Create CloudWatch Log Group**: Create log group `/aws/lambda/mcp-google-search` with 1-week retention.

#### CDK Infrastructure - AgentCore Gateway
- [ ] **Define AgentCore Gateway**: Create `CfnGateway` with:
  - Name: `${projectPrefix}-mcp-gateway`
  - Description: MCP Gateway for custom tools
  - Role: Gateway execution role
  - Authorization type: `AWS_IAM` (SigV4 authentication)
  - Protocol type: `MCP`
  - Exception level: `DEBUG` for dev, `ERROR` for prod
  - MCP protocol configuration: Default settings
- [ ] **Store Gateway URL in SSM**: Write Gateway URL to `/${projectPrefix}/gateway/url`.
- [ ] **Store Gateway ID in SSM**: Write Gateway ID to `/${projectPrefix}/gateway/id`.
- [ ] **Add Gateway Outputs**: Export Gateway ARN, URL, ID, and status.

#### CDK Infrastructure - Gateway Targets
**Note**: Gateway Targets connect Lambda functions to the Gateway as MCP tools

- [ ] **Define Google Web Search Target**: Create `CfnGatewayTarget` for `google_web_search`:
  - Name: `google-web-search`
  - Gateway identifier: Reference to Gateway
  - Description: "Search the web using Google Custom Search API. Returns up to 5 high-quality results."
  - Credential provider: `GATEWAY_IAM_ROLE`
  - Target configuration: MCP Lambda target with:
    - Lambda ARN: `mcp-google-search` function
    - Tool schema: JSON Schema defining `query` parameter (string, required)
- [ ] **Define Google Image Search Target**: Create `CfnGatewayTarget` for `google_image_search`:
  - Name: `google-image-search`
  - Gateway identifier: Reference to Gateway
  - Description: "Search for images using Google's image search. Returns up to 5 verified accessible images."
  - Credential provider: `GATEWAY_IAM_ROLE`
  - Target configuration: MCP Lambda target with:
    - Lambda ARN: `mcp-google-search` function
    - Tool schema: JSON Schema defining `query` parameter (string, required)
- [ ] **Add Target Outputs**: Export total number of targets (2) and tool names.

#### Build & Deploy Scripts
- [ ] **Create Scripts Directory**: Set up `scripts/stack-gateway/`.
- [ ] **Script: Install Dependencies**: Create `scripts/stack-gateway/install.sh` to install CDK and Python dependencies.
- [ ] **Script: Build CDK**: Create `scripts/stack-gateway/build-cdk.sh` to compile TypeScript CDK code.
- [ ] **Script: Synthesize Stack**: Create `scripts/stack-gateway/synth.sh` to synthesize CloudFormation with all context parameters.
- [ ] **Script: Test CDK**: Create `scripts/stack-gateway/test-cdk.sh` to validate with `cdk diff`.
- [ ] **Script: Deploy Stack**: Create `scripts/stack-gateway/deploy.sh` to:
  - Check for pre-synthesized templates in `cdk.out/`
  - Deploy stack with explicit context parameters (CDK handles Lambda packaging automatically)
  - Validate Gateway is accessible after deployment
  - Output usage instructions for Runtime integration
- [ ] **Script: Test Gateway**: Create `scripts/stack-gateway/test.sh` to validate Gateway connectivity and list tools.

#### Integration with Inference API Stack (Phase 4)
- [ ] **Update Runtime Execution Role**: Add Gateway invoke permissions to Inference API Runtime:
  - `bedrock-agentcore:InvokeGateway`
  - `bedrock-agentcore:GetGateway`
  - `bedrock-agentcore:ListGateways`
  - Resources: `arn:aws:bedrock-agentcore:${region}:${account}:gateway/*`
- [ ] **Add Gateway URL to Runtime Environment**: Pass Gateway URL to AgentCore Runtime via SSM parameter lookup.
- [ ] **Update Agent Code**: Integrate Gateway client in `backend/src/apis/inference_api/` to invoke tools with SigV4 authentication.

#### CI/CD Pipeline (9-Job Modular Pattern)
- [ ] **Create Workflow File**: Create `.github/workflows/gateway.yml` using standard 9-job pattern:
  - **Job 1: install** - Install and cache CDK/Python dependencies
  - **Job 2: build-cdk** - Compile TypeScript CDK code
  - **Job 3: synth-cdk** - Synthesize CloudFormation templates, upload as artifact
  - **Job 4: test-cdk** - Validate templates with `cdk diff`
  - **Job 5: test-lambda** - Run Python unit tests for Lambda functions (if any)
  - **Job 6: deploy-stack** - Deploy CDK stack (CDK automatically packages Lambda functions)
  - **Job 7: test-gateway** - Validate Gateway connectivity and list tools
- [ ] **Configure Path Triggers**: Set `paths` filter to trigger on:
  - `infrastructure/lib/gateway-stack.ts`
  - `backend/lambda-functions/**`
  - `scripts/stack-gateway/**`
  - `.github/workflows/gateway.yml`
- [ ] **Configure Environment Variables**: Use same pattern as other stacks (`CDK_AWS_REGION`, `CDK_PROJECT_PREFIX`, etc.)
- [ ] **Configure AWS Credentials**: Use composite action `./.github/actions/configure-aws-credentials` with OIDC fallback
- [ ] **Add Concurrency Control**: Use `concurrency: { group: gateway-${{ github.ref }}, cancel-in-progress: false }`

#### Documentation & Testing
- [ ] **Update README**: Document Gateway stack in main README.md with architecture diagram.
- [ ] **Create Gateway Usage Guide**: Add section to README documenting how to:
  - **Set Google API Keys**: Create Secrets Manager secret before deployment:
    ```bash
    aws secretsmanager create-secret \
      --name "${CDK_PROJECT_PREFIX}/mcp/google-credentials" \
      --secret-string '{"api_key":"YOUR_API_KEY","search_engine_id":"YOUR_ENGINE_ID"}' \
      --description "Google Custom Search API credentials" \
      --region ${CDK_AWS_REGION}
    ```
  - **Get Google API Credentials**:
    - API Key: https://console.cloud.google.com/apis/credentials
    - Search Engine ID: https://programmablesearchengine.google.com/
  - **Add New Lambda-based MCP Tools**: Create directory, implement handler with tool routing, update CDK stack with new function and Gateway Targets
  - **Test Individual Lambda Functions Locally**: Use AWS SAM or Docker for local testing with mock events
  - **Test Gateway Connectivity**: Use AWS CLI to list tools and test invocation
  - **Integrate Gateway with AgentCore Runtime**: SigV4 authentication pattern with boto3 + botocore.auth.SigV4Auth
  - **Debug Lambda Function Issues**: Check CloudWatch Logs for errors, verify Secrets Manager permissions

**🔒 HUMAN APPROVAL REQUIRED**
- [ ] [HUMAN] Phase 5 verified and approved to proceed to Phase 6

### Phase 6: Local Orchestration
**Goal**: Interactive deployment script for local development

- [ ] **Create Orchestration Script**: Create `deploy.sh` in repository root.
- [ ] **Implement Menu System**: Add interactive menu with options: "1) Deploy Frontend", "2) Deploy App API", "3) Deploy Inference API", "4) Deploy Agent Core", "5) Deploy Gateway", "6) Deploy All", "7) Exit".
- [ ] **Implement Stack Deployment Functions**: Create functions that call the individual stack deploy scripts.
- [ ] **Add Environment Validation**: Check for required environment variables and AWS credentials before deploying.
- [ ] **Add Dry-Run Option**: Implement `--dry-run` flag to show what would be deployed without executing.
- [ ] **Add Logging**: Output clear status messages for each deployment step.
- [ ] **Make Script Executable**: Ensure script has proper shebang and execute permissions.
- [ ] **Test Local Deployment**: Verify the orchestration script works on local machine by running a test deployment.

---

## Completion Criteria

All tasks above must be marked as complete (`- [x]`). Each stack should be deployable independently and all CI/CD pipelines should trigger correctly based on path changes.
