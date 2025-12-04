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
- [ ] **Create CDK Project Structure**: Initialize CDK application root directory (`infrastructure/` or `cdk/`) with `cdk init app --language=typescript`.
- [ ] **Create cdk.context.json Template**: Define configurable context structure (projectPrefix, awsRegion, awsAccount, vpcCidr, domainName, etc.).
- [ ] **Create Config Loader Module**: Implement `infrastructure/lib/config.ts` to read from `cdk.context.json` and validate required values.
- [ ] **Create Common Scripts Directory**: Set up `scripts/common/` with `.gitkeep`.
- [ ] **Script: Environment Loader**: Create `scripts/common/load-env.sh` to export configuration as environment variables.
- [ ] **Script: Dependency Installer**: Create `scripts/common/install-deps.sh` to install Node.js, AWS CDK CLI, Python, pip, Docker.

**🔒 HUMAN APPROVAL REQUIRED**
- [ ] [HUMAN] Phase 0 verified and approved to proceed to Phase 1

### Phase 1: Frontend Stack (Static Site)
**Goal**: S3 + CloudFront + Route53 (Optional)

#### CDK Infrastructure
- [ ] **Create FrontendStack File**: Create `infrastructure/lib/frontend-stack.ts`.
- [ ] **Define S3 Bucket**: Configure S3 bucket with website hosting, block public access settings, and configurable bucket name using `${projectPrefix}-frontend-${awsAccount}`.
- [ ] **Define CloudFront Distribution**: Create CloudFront distribution with OAC (Origin Access Control), custom error responses, and configurable price class.
- [ ] **Add CloudFront Outputs**: Export CloudFront Distribution ID and Domain Name as CloudFormation Outputs.
- [ ] **Store Parameters in SSM**: Write Distribution ID and Website URL to SSM Parameter Store (`/${projectPrefix}/frontend/distribution-id` and `/${projectPrefix}/frontend/url`).
- [ ] **Optional Route53 Integration**: If domain name is configured, create Route53 A record aliasing to CloudFront.

#### Build & Deploy Scripts
- [ ] **Create Scripts Directory**: Set up `scripts/stack-frontend/`.
- [ ] **Script: Install Dependencies**: Create `scripts/stack-frontend/install.sh` to run `npm ci` in `frontend/ai.client`.
- [ ] **Script: Build Frontend**: Create `scripts/stack-frontend/build.sh` to run `ng build --configuration production`.
- [ ] **Script: Test Frontend**: Create `scripts/stack-frontend/test.sh` to run `ng test --watch=false`.
- [ ] **Script: Deploy CDK**: Create `scripts/stack-frontend/deploy-cdk.sh` to run `cdk deploy FrontendStack`.
- [ ] **Script: Deploy Assets**: Create `scripts/stack-frontend/deploy-assets.sh` to sync `dist/` to S3 and invalidate CloudFront cache.

#### CI/CD Pipeline
- [ ] **Create Workflow File**: Create `.github/workflows/frontend.yml`.
- [ ] **Configure Path Triggers**: Set `paths` filter to trigger on `frontend/**` changes.
- [ ] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [ ] **Add Build Step**: Call `scripts/stack-frontend/install.sh` then `scripts/stack-frontend/build.sh`.
- [ ] **Add Test Step**: Call `scripts/stack-frontend/test.sh`.
- [ ] **Add CDK Deploy Step**: Call `scripts/stack-frontend/deploy-cdk.sh`.
- [ ] **Add Asset Deploy Step**: Call `scripts/stack-frontend/deploy-assets.sh`.
- [ ] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [ ] [HUMAN] Phase 1 verified and approved to proceed to Phase 2

### Phase 2: App API Stack (Core Backend)
**Goal**: VPC + ALB + Fargate + RDS/DynamoDB

#### CDK Infrastructure - Networking
- [ ] **Create AppApiStack File**: Create `infrastructure/lib/app-api-stack.ts`.
- [ ] **Define VPC**: Create VPC with configurable CIDR, public/private subnets across 2+ AZs, NAT Gateways.
- [ ] **Export VPC to SSM**: Store VPC ID in `/${projectPrefix}/network/vpc-id`.
- [ ] **Export Subnets to SSM**: Store private subnet IDs in `/${projectPrefix}/network/private-subnet-ids`.
- [ ] **Export Public Subnets to SSM**: Store public subnet IDs in `/${projectPrefix}/network/public-subnet-ids`.

#### CDK Infrastructure - Data Layer
- [ ] **Define Security Groups**: Create security groups for ALB, ECS tasks, and RDS/DynamoDB access.
- [ ] **Define Database**: Create RDS Aurora Serverless v2 OR DynamoDB table based on configuration flag.
- [ ] **Export Database Connection Info**: Store connection string/endpoint in Secrets Manager and reference ARN in SSM.

#### CDK Infrastructure - Compute & Load Balancing
- [ ] **Define Application Load Balancer**: Create ALB in public subnets with security group.
- [ ] **Export ALB ARN to SSM**: Store ALB ARN in `/${projectPrefix}/network/alb-arn`.
- [ ] **Export ALB Listener ARN**: Store listener ARN in `/${projectPrefix}/network/alb-listener-arn`.
- [ ] **Define ECS Cluster**: Create Fargate cluster with configurable name.
- [ ] **Define ECR Repository**: Create ECR repository for App API Docker images.
- [ ] **Define Task Definition**: Create Fargate task definition with configurable CPU/memory, environment variables from config.
- [ ] **Define ECS Service**: Create Fargate service with auto-scaling, health checks, and ALB target group integration.
- [ ] **Add CloudFormation Outputs**: Export cluster name, service name, and task definition ARN.

#### Build & Deploy Scripts
- [ ] **Create Scripts Directory**: Set up `scripts/stack-app-api/`.
- [ ] **Script: Install Dependencies**: Create `scripts/stack-app-api/install.sh` to install Python dependencies via Poetry/pip.
- [ ] **Script: Build Docker Image**: Create `scripts/stack-app-api/build.sh` to build and tag Docker image.
- [ ] **Script: Run Tests**: Create `scripts/stack-app-api/test.sh` to run pytest.
- [ ] **Script: Deploy Infrastructure**: Create `scripts/stack-app-api/deploy.sh` to deploy CDK stack and push Docker image to ECR.

#### CI/CD Pipeline
- [ ] **Create Workflow File**: Create `.github/workflows/app-api.yml`.
- [ ] **Configure Path Triggers**: Set `paths` filter to trigger on `backend/src/apis/app_api/**` changes.
- [ ] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [ ] **Add Install Step**: Call `scripts/stack-app-api/install.sh`.
- [ ] **Add Build Step**: Call `scripts/stack-app-api/build.sh`.
- [ ] **Add Test Step**: Call `scripts/stack-app-api/test.sh`.
- [ ] **Add Deploy Step**: Call `scripts/stack-app-api/deploy.sh`.
- [ ] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [ ] [HUMAN] Phase 2 verified and approved to proceed to Phase 3

### Phase 3: Inference API Stack (AI Workloads)
**Goal**: Dedicated Fargate Tasks sharing Stack 2's Network

#### CDK Infrastructure
- [ ] **Create InferenceApiStack File**: Create `infrastructure/lib/inference-api-stack.ts`.
- [ ] **Import Network Resources**: Read VPC ID, Subnet IDs, ALB ARN, and Listener ARN from SSM Parameter Store.
- [ ] **Define ECR Repository**: Create ECR repository for Inference API Docker images.
- [ ] **Define Task Definition**: Create Fargate task definition with higher CPU/memory allocation for inference workloads, GPU support if configured.
- [ ] **Define Target Group**: Create ALB target group with health check path `/health`.
- [ ] **Define Listener Rule**: Add listener rule to ALB to route `/inference/**` to Inference API target group.
- [ ] **Define ECS Service**: Create Fargate service in the imported VPC, attaching to the new target group.
- [ ] **Add CloudFormation Outputs**: Export service name and task definition ARN.

#### Build & Deploy Scripts
- [ ] **Create Scripts Directory**: Set up `scripts/stack-inference-api/`.
- [ ] **Script: Install Dependencies**: Create `scripts/stack-inference-api/install.sh` to install Python dependencies.
- [ ] **Script: Build Docker Image**: Create `scripts/stack-inference-api/build.sh` to build and tag Docker image.
- [ ] **Script: Run Tests**: Create `scripts/stack-inference-api/test.sh` to run inference tests.
- [ ] **Script: Deploy Infrastructure**: Create `scripts/stack-inference-api/deploy.sh` to deploy CDK stack and push Docker image to ECR.

#### CI/CD Pipeline
- [ ] **Create Workflow File**: Create `.github/workflows/inference-api.yml`.
- [ ] **Configure Path Triggers**: Set `paths` filter to trigger on `backend/src/apis/inference_api/**` changes.
- [ ] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [ ] **Add Install Step**: Call `scripts/stack-inference-api/install.sh`.
- [ ] **Add Build Step**: Call `scripts/stack-inference-api/build.sh`.
- [ ] **Add Test Step**: Call `scripts/stack-inference-api/test.sh`.
- [ ] **Add Deploy Step**: Call `scripts/stack-inference-api/deploy.sh`.
- [ ] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [ ] [HUMAN] Phase 3 verified and approved to proceed to Phase 4

### Phase 4: Agent Core Stack (Managed Services)
**Goal**: Serverless/Managed infrastructure for agents

#### CDK Infrastructure
- [ ] **Create AgentCoreStack File**: Create `infrastructure/lib/agent-core-stack.ts`.
- [ ] **Define Agent Storage**: Create DynamoDB table for agent state/configuration or S3 bucket for agent artifacts.
- [ ] **Define Lambda Functions**: Create Lambda functions for agent orchestration (if using serverless model).
- [ ] **Define Step Functions**: Create Step Functions state machine for agent workflow execution (if applicable).
- [ ] **Define IAM Roles**: Create execution roles with least-privilege access to required services.
- [ ] **Export Agent Resources**: Store resource ARNs in SSM Parameter Store under `/${projectPrefix}/agents/`.
- [ ] **Add CloudFormation Outputs**: Export Lambda function names and Step Function ARN.

#### Build & Deploy Scripts
- [ ] **Create Scripts Directory**: Set up `scripts/stack-agent-core/`.
- [ ] **Script: Install Dependencies**: Create `scripts/stack-agent-core/install.sh` to install agent dependencies.
- [ ] **Script: Run Tests**: Create `scripts/stack-agent-core/test.sh` to run agent tests.
- [ ] **Script: Deploy Infrastructure**: Create `scripts/stack-agent-core/deploy.sh` to deploy CDK stack.

#### CI/CD Pipeline
- [ ] **Create Workflow File**: Create `.github/workflows/agent-core.yml`.
- [ ] **Configure Path Triggers**: Set `paths` filter to trigger on `backend/src/agents/**` changes.
- [ ] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [ ] **Add Install Step**: Call `scripts/stack-agent-core/install.sh`.
- [ ] **Add Test Step**: Call `scripts/stack-agent-core/test.sh`.
- [ ] **Add Deploy Step**: Call `scripts/stack-agent-core/deploy.sh`.
- [ ] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

**🔒 HUMAN APPROVAL REQUIRED**
- [ ] [HUMAN] Phase 4 verified and approved to proceed to Phase 5

### Phase 5: Gateway & MCP Stack
**Goal**: API Gateway / Entry point

#### CDK Infrastructure
- [ ] **Create GatewayStack File**: Create `infrastructure/lib/gateway-stack.ts`.
- [ ] **Define API Gateway**: Create REST API or HTTP API Gateway with custom domain (if configured).
- [ ] **Define Gateway Integrations**: Set up integrations to App API and Inference API ALB/services.
- [ ] **Define CORS Configuration**: Configure CORS based on frontend URL from config.
- [ ] **Define WAF Rules**: Add AWS WAF web ACL for API Gateway (optional, based on config).
- [ ] **Export Gateway URL**: Store API Gateway URL in SSM Parameter Store at `/${projectPrefix}/gateway/url`.
- [ ] **Add CloudFormation Outputs**: Export API Gateway ID and endpoint URL.

#### Build & Deploy Scripts
- [ ] **Create Scripts Directory**: Set up `scripts/stack-gateway/`.
- [ ] **Script: Deploy Infrastructure**: Create `scripts/stack-gateway/deploy.sh` to deploy CDK stack.

#### CI/CD Pipeline
- [ ] **Create Workflow File**: Create `.github/workflows/gateway.yml`.
- [ ] **Configure Path Triggers**: Set `paths` filter to trigger on `infrastructure/lib/gateway-stack.ts` changes.
- [ ] **Add Dependency Installation Step**: Call `scripts/common/install-deps.sh`.
- [ ] **Add Deploy Step**: Call `scripts/stack-gateway/deploy.sh`.
- [ ] **Configure AWS Credentials**: Use GitHub OIDC or AWS credentials from secrets.

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
