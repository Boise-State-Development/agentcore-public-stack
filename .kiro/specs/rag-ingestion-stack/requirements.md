# Requirements Document: RAG Ingestion Stack

## Introduction

This document specifies the requirements for creating a new independent RAG (Retrieval-Augmented Generation) ingestion stack that is a carbon copy of the existing AppApiStack RAG implementation, but deployed as a separate, modular stack. The new stack will reuse the same Dockerfile (`backend/Dockerfile.rag-ingestion`) and implementation code, establishing an identical parallel deployment that can be verified before migrating away from the AppApiStack implementation.

**Implementation Strategy:**
- Create NEW stack with IDENTICAL functionality to existing RAG implementation
- Reuse SAME Dockerfile and code (no code changes)
- Deploy as SEPARATE stack with DISTINCT resource names
- Existing AppApiStack RAG resources remain UNCHANGED and OPERATIONAL
- Once verified, existing RAG resources can be removed from AppApiStack in a future phase

**Important:** This spec creates NEW resources in a NEW stack using the SAME implementation. The existing RAG resources in AppApiStack will remain operational and unchanged. No resources will be removed from AppApiStack as part of this work.

## Glossary

- **RAG_Ingestion_Stack**: The new independent CDK stack that owns all RAG-related AWS resources
- **App_API_Stack**: The existing application backend stack that currently contains RAG resources
- **Infrastructure_Stack**: The foundation stack that provides VPC, ALB, and ECS cluster
- **SSM_Parameter_Store**: AWS Systems Manager Parameter Store used for cross-stack resource references
- **Vector_Store**: AWS S3 Vectors service for storing and querying embeddings
- **Ingestion_Lambda**: Docker-based Lambda function that processes documents and generates embeddings
- **Documents_Bucket**: S3 bucket where users upload documents for RAG processing
- **Assistants_Table**: DynamoDB table storing assistant metadata
- **CI_CD_Pipeline**: GitHub Actions workflow for automated build, test, and deployment
- **ECR_Repository**: Amazon Elastic Container Registry for storing Docker images
- **ARM64_Lambda**: Lambda function running on ARM64 (Graviton2) architecture
- **Bedrock_Embeddings**: AWS Bedrock Titan embedding model for generating vector embeddings

## Requirements

### Requirement 1: Independent Stack Creation

**User Story:** As a DevOps engineer, I want the RAG ingestion pipeline in its own CDK stack, so that I can deploy and manage it independently from the application API.

#### Acceptance Criteria

1. THE RAG_Ingestion_Stack SHALL be defined in a separate TypeScript file `infrastructure/lib/rag-ingestion-stack.ts`
2. THE RAG_Ingestion_Stack SHALL import network resources from Infrastructure_Stack via SSM_Parameter_Store
3. THE RAG_Ingestion_Stack SHALL NOT have direct CloudFormation cross-stack references to App_API_Stack
4. WHEN RAG_Ingestion_Stack is deployed, THEN it SHALL create all RAG-related resources independently
5. THE RAG_Ingestion_Stack SHALL be registered in `infrastructure/bin/infrastructure.ts` for CDK synthesis

### Requirement 2: New Resource Creation

**User Story:** As a cloud architect, I want new RAG resources created in RagIngestionStack, so that the target architecture is established without disrupting existing functionality.

#### Acceptance Criteria

1. THE RAG_Ingestion_Stack SHALL create a NEW Documents_Bucket (S3 bucket for document uploads)
2. THE RAG_Ingestion_Stack SHALL create a NEW Vector_Store bucket and index (S3 Vectors resources)
3. THE RAG_Ingestion_Stack SHALL create a NEW Assistants_Table (DynamoDB table)
4. THE RAG_Ingestion_Stack SHALL create a NEW Ingestion_Lambda function (Docker-based Lambda)
5. THE RAG_Ingestion_Stack SHALL configure all IAM permissions for its resources
6. THE RAG_Ingestion_Stack SHALL configure S3 event notifications to trigger its Ingestion_Lambda
7. THE RAG_Ingestion_Stack SHALL configure CORS settings on its Documents_Bucket matching original configuration
8. THE App_API_Stack SHALL continue to own and operate its existing RAG resources unchanged
9. THE existing RAG resources in App_API_Stack SHALL remain functional during and after deployment
10. THE new RAG resources SHALL use distinct names to avoid conflicts (e.g., suffix "-v2" or "-new")

### Requirement 3: Cross-Stack Communication via SSM

**User Story:** As a systems architect, I want resource references shared via SSM Parameter Store, so that stacks remain loosely coupled and independently deployable.

#### Acceptance Criteria

1. THE RAG_Ingestion_Stack SHALL export Documents_Bucket name to SSM at `/${projectPrefix}/rag/documents-bucket-name`
2. THE RAG_Ingestion_Stack SHALL export Documents_Bucket ARN to SSM at `/${projectPrefix}/rag/documents-bucket-arn`
3. THE RAG_Ingestion_Stack SHALL export Assistants_Table name to SSM at `/${projectPrefix}/rag/assistants-table-name`
4. THE RAG_Ingestion_Stack SHALL export Assistants_Table ARN to SSM at `/${projectPrefix}/rag/assistants-table-arn`
5. THE RAG_Ingestion_Stack SHALL export Vector_Store bucket name to SSM at `/${projectPrefix}/rag/vector-bucket-name`
6. THE RAG_Ingestion_Stack SHALL export Vector_Store index name to SSM at `/${projectPrefix}/rag/vector-index-name`
7. THE RAG_Ingestion_Stack SHALL export Ingestion_Lambda ARN to SSM at `/${projectPrefix}/rag/ingestion-lambda-arn`
8. WHEN App_API_Stack needs RAG resource names, THEN it SHALL import them from SSM_Parameter_Store
9. THE App_API_Stack SHALL NOT hardcode any RAG resource names or ARNs

### Requirement 4: Configuration Management

**User Story:** As a developer, I want RAG-specific configuration centralized in config.ts, so that all settings are discoverable and follow project conventions.

#### Acceptance Criteria

1. THE config.ts SHALL define a RagIngestionConfig interface with all RAG-specific settings
2. THE RagIngestionConfig SHALL include enabled flag (boolean)
3. THE RagIngestionConfig SHALL include corsOrigins (comma-separated string)
4. THE RagIngestionConfig SHALL include lambdaMemorySize (number, default 10240 MB)
5. THE RagIngestionConfig SHALL include lambdaTimeout (number, default 900 seconds)
6. THE RagIngestionConfig SHALL include embeddingModel (string, default "amazon.titan-embed-text-v2")
7. THE RagIngestionConfig SHALL include vectorDimension (number, default 1024)
8. THE loadConfig function SHALL load RagIngestionConfig from environment variables with context fallback
9. WHEN environment variable CDK_RAG_ENABLED is set, THEN it SHALL override context value
10. WHEN environment variable CDK_RAG_CORS_ORIGINS is set, THEN it SHALL override context value

### Requirement 5: Docker Build and ECR Management

**User Story:** As a CI/CD engineer, I want to reuse the existing RAG Lambda Dockerfile, so that the implementation is identical to the current working version.

#### Acceptance Criteria

1. THE CI_CD_Pipeline SHALL build Docker image from EXISTING `backend/Dockerfile.rag-ingestion`
2. THE CI_CD_Pipeline SHALL NOT modify the Dockerfile
3. THE CI_CD_Pipeline SHALL tag Docker image with git commit SHA
4. THE CI_CD_Pipeline SHALL export Docker image as tar artifact for job handover
5. THE CI_CD_Pipeline SHALL push Docker image to a NEW ECR_Repository
6. THE CI_CD_Pipeline SHALL create ECR_Repository if it does not exist
7. THE CI_CD_Pipeline SHALL store image tag in SSM at `/${projectPrefix}/rag-ingestion/image-tag`
8. WHEN CDK synthesizes RAG_Ingestion_Stack, THEN it SHALL read image tag from SSM_Parameter_Store
9. THE Docker build SHALL use ARM64_Lambda architecture (ubuntu-24.04-arm runner)
10. THE ECR_Repository SHALL be named `${projectPrefix}-rag-ingestion` (distinct from existing repo)
11. THE Dockerfile SHALL remain shared between old and new implementations

### Requirement 6: CI/CD Workflow Structure

**User Story:** As a DevOps engineer, I want a modular GitHub Actions workflow for RAG ingestion, so that it follows project conventions and enables parallel execution.

#### Acceptance Criteria

1. THE CI_CD_Pipeline SHALL be defined in `.github/workflows/rag-ingestion.yml`
2. THE CI_CD_Pipeline SHALL have an install job that caches dependencies
3. THE CI_CD_Pipeline SHALL have a build-docker job that builds and exports the Docker image
4. THE CI_CD_Pipeline SHALL have a build-cdk job that compiles TypeScript CDK code
5. THE CI_CD_Pipeline SHALL have a test-docker job that validates the Docker image
6. THE CI_CD_Pipeline SHALL have a test-cdk job that validates CloudFormation templates
7. THE CI_CD_Pipeline SHALL have a synth-cdk job that synthesizes CloudFormation templates
8. THE CI_CD_Pipeline SHALL have a push-to-ecr job that pushes Docker image to ECR
9. THE CI_CD_Pipeline SHALL have a deploy-infrastructure job that deploys the CDK stack
10. WHEN build-docker and build-cdk jobs complete, THEN test jobs SHALL run in parallel
11. WHEN all tests pass, THEN synth-cdk and push-to-ecr jobs SHALL run in parallel
12. WHEN synth-cdk and push-to-ecr complete, THEN deploy-infrastructure job SHALL run

### Requirement 7: Script-Based Automation

**User Story:** As a developer, I want all CI/CD logic in shell scripts, so that I can reproduce builds and deployments locally.

#### Acceptance Criteria

1. THE scripts SHALL be located in `scripts/stack-rag-ingestion/` directory
2. THE scripts SHALL include `install.sh` for dependency installation
3. THE scripts SHALL include `build.sh` for Docker image building
4. THE scripts SHALL include `build-cdk.sh` for TypeScript compilation
5. THE scripts SHALL include `synth.sh` for CDK template synthesis
6. THE scripts SHALL include `deploy.sh` for CDK stack deployment
7. THE scripts SHALL include `test-docker.sh` for Docker image validation
8. THE scripts SHALL include `test-cdk.sh` for CloudFormation template validation
9. THE scripts SHALL include `push-to-ecr.sh` for ECR image push
10. THE scripts SHALL include `tag-latest.sh` for tagging latest image
11. WHEN any script is executed, THEN it SHALL source `scripts/common/load-env.sh` for configuration
12. WHEN any script fails, THEN it SHALL exit with non-zero status code
13. THE scripts SHALL use `set -euo pipefail` for error handling
14. THE scripts SHALL be executable locally and in CI environments

### Requirement 8: Deployment Order and Dependencies

**User Story:** As a deployment engineer, I want clear deployment dependencies, so that stacks deploy in the correct order without failures.

#### Acceptance Criteria

1. THE RAG_Ingestion_Stack SHALL depend on Infrastructure_Stack (VPC, subnets)
2. THE RAG_Ingestion_Stack SHALL NOT depend on App_API_Stack
3. THE App_API_Stack SHALL NOT depend on RAG_Ingestion_Stack
4. WHEN Infrastructure_Stack is deployed, THEN RAG_Ingestion_Stack MAY be deployed
5. WHEN RAG_Ingestion_Stack is deployed, THEN App_API_Stack MAY be deployed in parallel
6. THE deployment order SHALL be: Infrastructure_Stack → (RAG_Ingestion_Stack || App_API_Stack)

### Requirement 9: Lambda Function Configuration

**User Story:** As a backend engineer, I want the RAG ingestion Lambda properly configured, so that it can process documents efficiently and reliably.

#### Acceptance Criteria

1. THE Ingestion_Lambda SHALL use Docker container image from ECR
2. THE Ingestion_Lambda SHALL use ARM64_Lambda architecture
3. THE Ingestion_Lambda SHALL have 10GB memory allocation (10240 MB)
4. THE Ingestion_Lambda SHALL have 15-minute timeout (900 seconds)
5. THE Ingestion_Lambda SHALL have environment variable ASSISTANTS_DOCUMENTS_BUCKET_NAME
6. THE Ingestion_Lambda SHALL have environment variable ASSISTANTS_TABLE_NAME
7. THE Ingestion_Lambda SHALL have environment variable ASSISTANTS_VECTOR_STORE_BUCKET_NAME
8. THE Ingestion_Lambda SHALL have environment variable ASSISTANTS_VECTOR_STORE_INDEX_NAME
9. THE Ingestion_Lambda SHALL have environment variable BEDROCK_REGION
10. THE Ingestion_Lambda SHALL have IAM permission to read from Documents_Bucket
11. THE Ingestion_Lambda SHALL have IAM permission to read/write to Assistants_Table
12. THE Ingestion_Lambda SHALL have IAM permission to invoke Bedrock embedding models
13. THE Ingestion_Lambda SHALL have IAM permission to write vectors to Vector_Store
14. WHEN an object is created in Documents_Bucket with prefix "assistants/", THEN Ingestion_Lambda SHALL be triggered

### Requirement 10: Vector Store Configuration

**User Story:** As a machine learning engineer, I want the vector store properly configured for Titan embeddings, so that semantic search works correctly.

#### Acceptance Criteria

1. THE Vector_Store SHALL be created as AWS::S3Vectors::VectorBucket resource
2. THE Vector_Store SHALL have a vector index as AWS::S3Vectors::Index resource
3. THE vector index SHALL use float32 data type
4. THE vector index SHALL use 1024 dimensions (Titan V2 embedding size)
5. THE vector index SHALL use cosine distance metric
6. THE vector index SHALL mark "text" metadata key as non-filterable
7. THE vector index SHALL allow filtering on "assistant_id", "document_id", and "source" metadata keys
8. THE vector index SHALL depend on Vector_Store bucket creation

### Requirement 11: DynamoDB Table Configuration

**User Story:** As a database administrator, I want the Assistants table properly configured, so that assistant metadata is stored efficiently.

#### Acceptance Criteria

1. THE Assistants_Table SHALL have partition key "PK" (String)
2. THE Assistants_Table SHALL have sort key "SK" (String)
3. THE Assistants_Table SHALL use PAY_PER_REQUEST billing mode
4. THE Assistants_Table SHALL have point-in-time recovery enabled
5. THE Assistants_Table SHALL have AWS_MANAGED encryption
6. THE Assistants_Table SHALL have OwnerStatusIndex GSI with GSI_PK and GSI_SK
7. THE Assistants_Table SHALL have VisibilityStatusIndex GSI with GSI2_PK and GSI2_SK
8. THE Assistants_Table SHALL have SharedWithIndex GSI with GSI3_PK and GSI3_SK
9. WHEN environment is "prod", THEN Assistants_Table SHALL have RETAIN removal policy
10. WHEN environment is not "prod", THEN Assistants_Table SHALL have DESTROY removal policy

### Requirement 12: S3 Bucket Configuration

**User Story:** As a security engineer, I want the documents bucket properly secured, so that user data is protected and accessible only via pre-signed URLs.

#### Acceptance Criteria

1. THE Documents_Bucket SHALL have S3_MANAGED encryption
2. THE Documents_Bucket SHALL have BLOCK_ALL public access
3. THE Documents_Bucket SHALL have versioning enabled
4. THE Documents_Bucket SHALL have RETAIN removal policy
5. THE Documents_Bucket SHALL have autoDeleteObjects disabled
6. THE Documents_Bucket SHALL have CORS configuration for browser uploads
7. THE CORS configuration SHALL allow GET, PUT, and HEAD methods
8. THE CORS configuration SHALL allow Content-Type, Content-Length, and x-amz-* headers
9. THE CORS configuration SHALL expose ETag, Content-Length, and Content-Type headers
10. THE CORS configuration SHALL have 3600 second max age
11. WHEN CDK_RAG_CORS_ORIGINS is set, THEN CORS SHALL use those origins
12. WHEN CDK_RAG_CORS_ORIGINS is not set, THEN CORS SHALL use default origins from config

### Requirement 13: Workflow Triggers and Paths

**User Story:** As a CI/CD engineer, I want the workflow to trigger on relevant changes, so that deployments happen automatically when needed.

#### Acceptance Criteria

1. THE CI_CD_Pipeline SHALL trigger on push to main branch
2. THE CI_CD_Pipeline SHALL trigger on pull requests
3. THE CI_CD_Pipeline SHALL trigger on workflow_dispatch (manual trigger)
4. WHEN files in `backend/src/rag/` change, THEN CI_CD_Pipeline SHALL run
5. WHEN files in `backend/Dockerfile.rag-ingestion` change, THEN CI_CD_Pipeline SHALL run
6. WHEN files in `infrastructure/lib/rag-ingestion-stack.ts` change, THEN CI_CD_Pipeline SHALL run
7. WHEN files in `scripts/stack-rag-ingestion/` change, THEN CI_CD_Pipeline SHALL run
8. WHEN files in `.github/workflows/rag-ingestion.yml` change, THEN CI_CD_Pipeline SHALL run
9. WHEN workflow_dispatch has skip_tests input, THEN test jobs SHALL be skipped if true
10. WHEN workflow_dispatch has skip_deploy input, THEN deploy job SHALL be skipped if true

### Requirement 14: Environment Variable Configuration

**User Story:** As a configuration manager, I want all configuration values passed via environment variables, so that secrets and settings are managed securely.

#### Acceptance Criteria

1. THE CI_CD_Pipeline SHALL define CDK_AWS_REGION from GitHub Variables
2. THE CI_CD_Pipeline SHALL define CDK_PROJECT_PREFIX from GitHub Variables
3. THE CI_CD_Pipeline SHALL define CDK_VPC_CIDR from GitHub Variables
4. THE CI_CD_Pipeline SHALL define CDK_RAG_ENABLED from GitHub Variables
5. THE CI_CD_Pipeline SHALL define CDK_RAG_CORS_ORIGINS from GitHub Variables
6. THE CI_CD_Pipeline SHALL define CDK_AWS_ACCOUNT from GitHub Secrets
7. THE CI_CD_Pipeline SHALL define AWS_ROLE_ARN from GitHub Secrets
8. THE CI_CD_Pipeline SHALL define AWS_ACCESS_KEY_ID from GitHub Secrets
9. THE CI_CD_Pipeline SHALL define AWS_SECRET_ACCESS_KEY from GitHub Secrets
10. THE CI_CD_Pipeline SHALL define CDK_REQUIRE_APPROVAL with default "never"

### Requirement 15: AppApiStack Optional Integration

**User Story:** As an application developer, I want the option to use new RAG resources from AppApiStack, so that I can test the new stack without breaking existing functionality.

#### Acceptance Criteria

1. THE App_API_Stack MAY optionally import new RAG resource names from SSM
2. THE App_API_Stack SHALL continue using its existing RAG resources by default
3. THE App_API_Stack SHALL have a configuration flag to switch between old and new RAG resources
4. WHEN using new RAG resources, THEN App_API_Stack SHALL import Documents_Bucket name from SSM at `/${projectPrefix}/rag/documents-bucket-name`
5. WHEN using new RAG resources, THEN App_API_Stack SHALL import Assistants_Table name from SSM at `/${projectPrefix}/rag/assistants-table-name`
6. WHEN using new RAG resources, THEN App_API_Stack SHALL import Vector_Store bucket name from SSM at `/${projectPrefix}/rag/vector-bucket-name`
7. WHEN using new RAG resources, THEN App_API_Stack SHALL import Vector_Store index name from SSM at `/${projectPrefix}/rag/vector-index-name`
8. THE App_API_Stack SHALL NOT be modified as part of this initial implementation
9. THE integration with App_API_Stack SHALL be deferred to a future migration phase
10. THE new RAG_Ingestion_Stack SHALL be independently testable without App_API_Stack changes

### Requirement 16: Testing Requirements

**User Story:** As a quality engineer, I want comprehensive tests for the RAG stack, so that deployments are validated before production.

#### Acceptance Criteria

1. THE test-docker job SHALL verify Docker image can start successfully
2. THE test-docker job SHALL verify Lambda handler is present in image
3. THE test-docker job SHALL verify required Python packages are installed
4. THE test-cdk job SHALL validate CloudFormation template syntax
5. THE test-cdk job SHALL verify all required resources are present in template
6. THE test-cdk job SHALL verify SSM parameter exports are correct
7. THE test-cdk job SHALL verify IAM permissions are properly configured
8. WHEN any test fails, THEN deployment SHALL NOT proceed
9. WHEN skip_tests input is true, THEN test jobs SHALL be skipped

### Requirement 17: Artifact Management

**User Story:** As a CI/CD engineer, I want artifacts properly managed between jobs, so that builds are reproducible and efficient.

#### Acceptance Criteria

1. THE install job SHALL cache Python packages with key based on pyproject.toml hash
2. THE install job SHALL cache node_modules with key based on package-lock.json hash
3. THE build-docker job SHALL export Docker image as tar artifact
4. THE build-docker job SHALL upload Docker image artifact with 1-day retention
5. THE synth-cdk job SHALL upload synthesized templates with 7-day retention
6. THE test-docker job SHALL download and load Docker image artifact
7. THE push-to-ecr job SHALL download and load Docker image artifact
8. THE deploy-infrastructure job SHALL download synthesized templates
9. WHEN artifacts are missing, THEN dependent jobs SHALL fail with clear error message

### Requirement 18: Deployment Outputs and Monitoring

**User Story:** As an operations engineer, I want deployment outputs captured, so that I can verify successful deployments and troubleshoot issues.

#### Acceptance Criteria

1. THE deploy-infrastructure job SHALL output CDK stack outputs to JSON file
2. THE deploy-infrastructure job SHALL upload deployment outputs as artifact with 30-day retention
3. THE deploy-infrastructure job SHALL create GitHub step summary with deployment details
4. THE deployment summary SHALL include AWS region
5. THE deployment summary SHALL include project prefix
6. THE deployment summary SHALL include stack name
7. THE deployment summary SHALL include Docker image tag
8. THE deployment summary SHALL include stack outputs in JSON format
9. WHEN deployment succeeds, THEN summary SHALL show success indicator
10. WHEN deployment fails, THEN error details SHALL be visible in logs

### Requirement 19: Concurrency Control

**User Story:** As a deployment engineer, I want deployment concurrency controlled, so that parallel deployments don't cause conflicts.

#### Acceptance Criteria

1. THE CI_CD_Pipeline SHALL use concurrency group "rag-ingestion-${{ github.ref }}"
2. THE CI_CD_Pipeline SHALL NOT cancel in-progress deployments
3. WHEN a deployment is running, THEN new deployments SHALL wait
4. WHEN a deployment completes, THEN queued deployments SHALL proceed

### Requirement 20: Documentation and Naming Conventions

**User Story:** As a new team member, I want consistent naming conventions, so that I can understand the codebase quickly.

#### Acceptance Criteria

1. THE CDK stack class SHALL be named "RagIngestionStack"
2. THE CDK stack file SHALL be named "rag-ingestion-stack.ts"
3. THE workflow file SHALL be named "rag-ingestion.yml"
4. THE scripts directory SHALL be named "stack-rag-ingestion"
5. THE ECR repository SHALL be named "${projectPrefix}-rag-ingestion"
6. THE SSM parameters SHALL use prefix "/${projectPrefix}/rag/"
7. THE CloudFormation outputs SHALL use prefix "RagIngestion"
8. THE resource names SHALL use getResourceName(config, "rag-*")
9. THE environment variables SHALL use prefix "CDK_RAG_" for CDK config
10. THE environment variables SHALL use prefix "ENV_RAG_" for runtime config

### Requirement 21: Non-Interference and Code Reuse

**User Story:** As a platform engineer, I want the new RAG stack to be a carbon copy using the same code, so that I can verify identical functionality before migration.

#### Acceptance Criteria

1. THE RAG_Ingestion_Stack SHALL NOT modify any existing AppApiStack resources
2. THE RAG_Ingestion_Stack SHALL NOT delete any existing AppApiStack resources
3. THE RAG_Ingestion_Stack SHALL use distinct resource names to avoid conflicts
4. THE RAG_Ingestion_Stack SHALL reuse the SAME Dockerfile as AppApiStack (`backend/Dockerfile.rag-ingestion`)
5. THE RAG_Ingestion_Stack SHALL reuse the SAME Lambda handler code as AppApiStack
6. THE RAG_Ingestion_Stack SHALL use the SAME configuration values as AppApiStack (memory, timeout, etc.)
7. THE RAG_Ingestion_Stack SHALL use the SAME IAM permissions as AppApiStack
8. THE RAG_Ingestion_Stack SHALL use the SAME environment variables as AppApiStack (with new resource names)
9. THE new Documents_Bucket SHALL have a different name than the existing assistants documents bucket
10. THE new Assistants_Table SHALL have a different name than the existing assistants table
11. THE new Vector_Store SHALL have a different name than the existing vector store
12. THE new Ingestion_Lambda SHALL have a different name than the existing ingestion lambda
13. THE new ECR_Repository SHALL have a different name than any existing repositories
14. WHEN RAG_Ingestion_Stack is deployed, THEN existing RAG functionality SHALL continue working
15. WHEN RAG_Ingestion_Stack is deleted, THEN existing RAG functionality SHALL remain unaffected
16. THE deployment of RAG_Ingestion_Stack SHALL NOT require changes to App_API_Stack
17. THE deployment of RAG_Ingestion_Stack SHALL NOT require redeployment of App_API_Stack
18. THE implementation SHALL be functionally identical to the existing AppApiStack RAG implementation
