# Implementation Plan: RAG Ingestion Stack

## Overview

This implementation plan creates a new independent RAG ingestion stack that is a carbon copy of the existing AppApiStack RAG implementation. The new stack will reuse the same Dockerfile and code, but deploy as a separate modular stack with distinct resource names and its own CI/CD pipeline.

**Key Principles:**
- Reuse existing `backend/Dockerfile.rag-ingestion` without modifications
- Create new resources with "rag-" prefix (distinct from "assistants-" prefix)
- Follow project DevOps conventions (SSM parameters, script-based automation)
- Deploy independently without affecting existing AppApiStack resources

## Tasks

- [x] 1. Add RAG Ingestion configuration to config.ts
  - Add RagIngestionConfig interface to config.ts
  - Add ragIngestion field to AppConfig interface
  - Implement configuration loading with env var precedence
  - Add validation for RAG configuration values
  - _Requirements: 4.1-4.10_

- [x] 2. Create RagIngestionStack CDK code
  - [x] 2.1 Create infrastructure/lib/rag-ingestion-stack.ts file
    - Define RagIngestionStack class extending cdk.Stack
    - Define RagIngestionStackProps interface
    - Import VPC and network resources from Infrastructure Stack via SSM
    - Apply standard tags using applyStandardTags helper
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 2.2 Create S3 Documents Bucket
    - Create S3 bucket with name `${projectPrefix}-rag-documents`
    - Configure S3_MANAGED encryption
    - Configure BLOCK_ALL public access
    - Enable versioning
    - Set RETAIN removal policy
    - Configure CORS from config.ragIngestion.corsOrigins
    - _Requirements: 2.1, 12.1-12.12_

  - [x] 2.3 Create S3 Vectors Bucket and Index
    - Create CfnResource for AWS::S3Vectors::VectorBucket
    - Set bucket name to `${projectPrefix}-rag-vector-store-v1`
    - Create CfnResource for AWS::S3Vectors::Index
    - Set index name to `${projectPrefix}-rag-vector-index-v1`
    - Configure float32 data type, 1024 dimensions, cosine metric
    - Configure metadata (filterable: assistant_id, document_id, source; non-filterable: text)
    - Add dependency: index depends on bucket
    - _Requirements: 2.2, 10.1-10.8_

  - [x] 2.4 Create DynamoDB Assistants Table
    - Create DynamoDB table with name `${projectPrefix}-rag-assistants`
    - Configure PK (String) and SK (String) keys
    - Set PAY_PER_REQUEST billing mode
    - Enable point-in-time recovery
    - Set AWS_MANAGED encryption
    - Add OwnerStatusIndex GSI (GSI_PK, GSI_SK)
    - Add VisibilityStatusIndex GSI (GSI2_PK, GSI2_SK)
    - Add SharedWithIndex GSI (GSI3_PK, GSI3_SK)
    - Set removal policy based on environment (RETAIN for prod, DESTROY otherwise)
    - _Requirements: 2.3, 11.1-11.10_


  - [x] 2.5 Create Lambda Function
    - Reference ECR repository `${projectPrefix}-rag-ingestion`
    - Import image tag from SSM parameter `/${projectPrefix}/rag-ingestion/image-tag`
    - Create DockerImageFunction with ARM64 architecture
    - Set memory to config.ragIngestion.lambdaMemorySize (10240 MB)
    - Set timeout to config.ragIngestion.lambdaTimeout (900 seconds)
    - Configure environment variables (ASSISTANTS_DOCUMENTS_BUCKET_NAME, ASSISTANTS_TABLE_NAME, ASSISTANTS_VECTOR_STORE_BUCKET_NAME, ASSISTANTS_VECTOR_STORE_INDEX_NAME, BEDROCK_REGION)
    - _Requirements: 2.4, 9.1-9.14_

  - [x] 2.6 Configure IAM Permissions
    - Grant Lambda read permission on Documents Bucket
    - Grant Lambda read/write permission on Assistants Table
    - Add IAM policy for S3 Vectors operations (PutVectors, GetVectors, ListVectors, DeleteVector)
    - Add IAM policy for Bedrock InvokeModel on Titan embeddings
    - _Requirements: 2.5, 9.10-9.13_

  - [x] 2.7 Configure S3 Event Notifications
    - Add S3 event notification on Documents Bucket
    - Trigger Lambda on ObjectCreated events
    - Filter by prefix "assistants/"
    - _Requirements: 2.7, 9.14_

  - [x] 2.8 Export SSM Parameters
    - Export Documents Bucket name to `/${projectPrefix}/rag/documents-bucket-name`
    - Export Documents Bucket ARN to `/${projectPrefix}/rag/documents-bucket-arn`
    - Export Assistants Table name to `/${projectPrefix}/rag/assistants-table-name`
    - Export Assistants Table ARN to `/${projectPrefix}/rag/assistants-table-arn`
    - Export Vector Bucket name to `/${projectPrefix}/rag/vector-bucket-name`
    - Export Vector Index name to `/${projectPrefix}/rag/vector-index-name`
    - Export Lambda ARN to `/${projectPrefix}/rag/ingestion-lambda-arn`
    - _Requirements: 3.1-3.7_

  - [x] 2.9 Add CloudFormation Outputs
    - Output Documents Bucket name
    - Output Assistants Table name
    - Output Lambda function ARN
    - Output Vector Bucket name
    - Output Vector Index name
    - _Requirements: 18.1-18.10_

- [x] 3. Register stack in CDK app
  - Import RagIngestionStack in infrastructure/bin/infrastructure.ts
  - Instantiate RagIngestionStack with config
  - Ensure stack is synthesized when running cdk synth
  - _Requirements: 1.5_

- [x] 4. Create shell scripts for RAG ingestion stack
  - [x] 4.1 Create scripts/stack-rag-ingestion/install.sh
    - Source common/load-env.sh
    - Install Python dependencies from backend/pyproject.toml
    - Install Node.js dependencies from infrastructure/package.json
    - Verify installations
    - _Requirements: 7.1, 7.2_

  - [x] 4.2 Create scripts/stack-rag-ingestion/build.sh
    - Source common/load-env.sh
    - Build Docker image from backend/Dockerfile.rag-ingestion
    - Tag with IMAGE_TAG environment variable
    - Validate build success
    - _Requirements: 7.1, 7.3_

  - [x] 4.3 Create scripts/stack-rag-ingestion/build-cdk.sh
    - Source common/load-env.sh
    - Run npm run build in infrastructure/
    - Validate TypeScript compilation
    - _Requirements: 7.1, 7.4_

  - [x] 4.4 Create scripts/stack-rag-ingestion/synth.sh
    - Source common/load-env.sh
    - Build CDK context parameters using build_cdk_context_params()
    - Run cdk synth RagIngestionStack with context parameters
    - Output to infrastructure/cdk.out/
    - _Requirements: 7.1, 7.5_

  - [x] 4.5 Create scripts/stack-rag-ingestion/deploy.sh
    - Source common/load-env.sh
    - Check for pre-synthesized templates in cdk.out/
    - Bootstrap CDK if needed
    - Build CDK context parameters if synthesizing during deploy
    - Deploy RagIngestionStack
    - Output deployment results to cdk-outputs-rag-ingestion.json
    - _Requirements: 7.1, 7.6_

  - [x] 4.6 Create scripts/stack-rag-ingestion/test-docker.sh
    - Source common/load-env.sh
    - Load Docker image from tar or local
    - Run container
    - Verify Lambda handler exists
    - Verify Python packages installed
    - _Requirements: 7.1, 7.7_

  - [x] 4.7 Create scripts/stack-rag-ingestion/test-cdk.sh
    - Source common/load-env.sh
    - Validate CloudFormation template syntax
    - Check for required resources in template
    - Verify SSM parameter exports
    - _Requirements: 7.1, 7.8_

  - [x] 4.8 Create scripts/stack-rag-ingestion/push-to-ecr.sh
    - Source common/load-env.sh
    - Create ECR repository if it doesn't exist
    - Authenticate to ECR
    - Tag Docker image with ECR URI
    - Push image to ECR
    - Store image tag in SSM parameter `/${projectPrefix}/rag-ingestion/image-tag`
    - _Requirements: 7.1, 7.9, 5.1-5.11_

  - [x] 4.9 Create scripts/stack-rag-ingestion/tag-latest.sh
    - Source common/load-env.sh
    - Tag current image as latest
    - Push latest tag to ECR
    - _Requirements: 7.1, 7.10_

- [x] 5. Update common/load-env.sh for RAG configuration
  - Add CDK_RAG_ENABLED export with env var and context fallback
  - Add CDK_RAG_CORS_ORIGINS export with env var and context fallback
  - Add CDK_RAG_LAMBDA_MEMORY export with env var and context fallback
  - Add CDK_RAG_LAMBDA_TIMEOUT export with env var and context fallback
  - Add context parameters for RAG config in build_cdk_context_params()
  - _Requirements: 4.1-4.10_

- [x] 6. Create GitHub Actions workflow
  - [x] 6.1 Create .github/workflows/rag-ingestion.yml
    - Define workflow name and triggers (push to main, pull requests, workflow_dispatch)
    - Configure path filters (backend/src/rag/, backend/Dockerfile.rag-ingestion, infrastructure/lib/rag-ingestion-stack.ts, scripts/stack-rag-ingestion/, .github/workflows/rag-ingestion.yml)
    - Define environment variables (CDK_AWS_REGION, CDK_PROJECT_PREFIX, CDK_VPC_CIDR, CDK_RAG_ENABLED, CDK_RAG_CORS_ORIGINS, CDK_AWS_ACCOUNT, AWS credentials)
    - Configure concurrency group "rag-ingestion-${{ github.ref }}"
    - _Requirements: 6.1, 13.1-13.10, 14.1-14.10, 19.1-19.4_

  - [x] 6.2 Add install job
    - Run on ubuntu-latest
    - Checkout code
    - Install system dependencies (scripts/common/install-deps.sh)
    - Install RAG dependencies (scripts/stack-rag-ingestion/install.sh)
    - Cache Python packages
    - Cache node_modules
    - _Requirements: 6.2, 17.1, 17.2_

  - [x] 6.3 Add build-docker job
    - Run on ubuntu-latest
    - Depend on install job
    - Checkout code
    - Set image tag from git commit SHA
    - Set up Docker Buildx
    - Build Docker image from backend/Dockerfile.rag-ingestion
    - Export image as tar artifact
    - Upload artifact with 1-day retention
    - Output image-tag
    - _Requirements: 6.3, 17.3, 17.4_

  - [x] 6.4 Add build-cdk job
    - Run on ubuntu-latest
    - Depend on install job
    - Checkout code
    - Restore node_modules cache
    - Build CDK (scripts/stack-rag-ingestion/build-cdk.sh)
    - _Requirements: 6.4_

  - [x] 6.5 Add test-docker job
    - Run on ubuntu-latest
    - Depend on build-docker job
    - Skip if skip_tests input is true
    - Checkout code
    - Download Docker image artifact
    - Load Docker image
    - Test Docker image (scripts/stack-rag-ingestion/test-docker.sh)
    - _Requirements: 6.5, 16.1-16.3, 17.6_

  - [x] 6.6 Add test-cdk job
    - Run on ubuntu-latest
    - Depend on synth-cdk job
    - Skip if skip_tests input is true
    - Checkout code
    - Restore node_modules cache
    - Download synthesized templates
    - Configure AWS credentials
    - Install system dependencies
    - Validate CloudFormation template (scripts/stack-rag-ingestion/test-cdk.sh)
    - _Requirements: 6.6, 16.4-16.7_

  - [x] 6.7 Add synth-cdk job
    - Run on ubuntu-24.04-arm (ARM64 runner for Lambda builds)
    - Depend on build-cdk job
    - Checkout code
    - Restore node_modules cache
    - Configure AWS credentials
    - Install system dependencies
    - Set up Docker Buildx
    - Synthesize CloudFormation template (scripts/stack-rag-ingestion/synth.sh)
    - Upload synthesized templates with 7-day retention
    - _Requirements: 6.7, 17.5_

  - [x] 6.8 Add push-to-ecr job
    - Run on ubuntu-latest
    - Depend on build-docker, test-docker jobs
    - Skip if any dependency failed
    - Checkout code
    - Download Docker image artifact
    - Load Docker image
    - Configure AWS credentials
    - Push to ECR (scripts/stack-rag-ingestion/push-to-ecr.sh)
    - Output image-tag
    - _Requirements: 6.8, 17.7_

  - [x] 6.9 Add deploy-infrastructure job
    - Run on ubuntu-24.04-arm (ARM64 runner)
    - Depend on test-cdk, push-to-ecr jobs
    - Skip if not push to main or workflow_dispatch
    - Skip if skip_deploy input is true
    - Checkout code
    - Restore node_modules cache
    - Download synthesized templates
    - Configure AWS credentials
    - Install system dependencies
    - Set up Docker Buildx
    - Deploy infrastructure (scripts/stack-rag-ingestion/deploy.sh)
    - Tag image as latest (scripts/stack-rag-ingestion/tag-latest.sh)
    - Upload deployment outputs with 30-day retention
    - Create deployment summary
    - _Requirements: 6.9, 17.8, 18.1-18.10_

- [x] 7. Checkpoint - Verify stack can be synthesized
  - Run cdk synth RagIngestionStack locally
  - Verify CloudFormation template generated
  - Check that all resources are present
  - Verify no cross-stack references to AppApiStack
  - Ensure all tests pass, ask the user if questions arise.


- [x] 8. Write CDK unit tests
  - [x] 8.1 Create infrastructure/test/rag-ingestion-stack.test.ts
    - Test S3 documents bucket configuration (encryption, versioning, CORS)
    - Test DynamoDB table configuration (keys, GSIs, billing mode)
    - Test Lambda function configuration (memory, timeout, environment variables)
    - Test IAM permissions (S3, DynamoDB, Bedrock, S3 Vectors)
    - Test SSM parameter exports (all 7 parameters)
    - Test CloudFormation outputs
    - _Requirements: 16.1-16.9_

  - [x] 8.2 Create infrastructure/test/config.test.ts for RAG config
    - Test configuration loading from environment variables
    - Test configuration fallback to context values
    - Test configuration defaults
    - Test configuration validation
    - _Requirements: 4.1-4.10_

- [x] 9. Write property-based tests
  - [x] 9.1 Write property test for CloudFormation template completeness
    - **Property 1: CloudFormation Template Completeness**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 2.8, 9.1-9.14, 10.1-10.8, 11.1-11.10, 12.1-12.12**
    - Generate random valid configurations (projectPrefix, awsRegion, corsOrigins)
    - Synthesize stack for each configuration
    - Verify all required resources present (S3 bucket, Vector bucket, Vector index, DynamoDB table, Lambda function)
    - Verify resource types correct
    - Run 100 iterations
    - _Requirements: 2.1-2.8, 9.1-9.14, 10.1-10.8, 11.1-11.10, 12.1-12.12_

  - [x] 9.2 Write property test for no cross-stack references
    - **Property 2: No Cross-Stack References**
    - **Validates: Requirements 1.3**
    - Generate random valid configurations
    - Synthesize stack for each configuration
    - Verify no Fn::ImportValue in template
    - Verify no references to AppApiStack
    - Run 100 iterations
    - _Requirements: 1.3_

  - [x] 9.3 Write property test for SSM parameter exports
    - **Property 3: SSM Parameter Exports**
    - **Validates: Requirements 3.1-3.7**
    - Generate random valid projectPrefix values
    - Synthesize stack for each configuration
    - Verify all 7 SSM parameters present
    - Verify parameter names follow pattern `/${projectPrefix}/rag/*`
    - Run 100 iterations
    - _Requirements: 3.1-3.7_

  - [x] 9.4 Write property test for configuration loading
    - **Property 4: Configuration Loading**
    - **Validates: Requirements 4.1-4.10**
    - Generate random combinations of environment variables and context values
    - Load configuration for each combination
    - Verify precedence: env > context > default
    - Verify all config fields loaded correctly
    - Run 100 iterations
    - _Requirements: 4.1-4.10_

  - [x] 9.5 Write property test for resource naming uniqueness
    - **Property 6: Resource Naming Uniqueness**
    - **Validates: Requirements 20.1-20.10, 21.1-21.18**
    - Generate random valid projectPrefix values
    - Synthesize stack for each configuration
    - Extract all resource names from template
    - Verify all names use "rag-" prefix
    - Verify no names use "assistants-" prefix
    - Run 100 iterations
    - _Requirements: 20.1-20.10, 21.1-21.18_

- [x] 10. Update cdk.context.json with RAG configuration
  - Add ragIngestion section to cdk.context.json
  - Set enabled: true
  - Set corsOrigins with appropriate values for environment
  - Set lambdaMemorySize: 10240
  - Set lambdaTimeout: 900
  - Set embeddingModel: "amazon.titan-embed-text-v2"
  - Set vectorDimension: 1024
  - Set vectorDistanceMetric: "cosine"
  - _Requirements: 4.1-4.10_

- [x] 11. Configure GitHub repository settings
  - Add GitHub Variable: CDK_RAG_ENABLED=true
  - Add GitHub Variable: CDK_RAG_CORS_ORIGINS (comma-separated origins)
  - Verify existing secrets are present (CDK_AWS_ACCOUNT, AWS_ROLE_ARN, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
  - _Requirements: 14.1-14.10_

- [x] 12. Checkpoint - Test full CI/CD pipeline
  - Create feature branch
  - Push changes to trigger workflow
  - Monitor workflow execution
  - Verify all jobs pass
  - Verify Docker image builds
  - Verify CDK synthesizes
  - Verify tests pass
  - Do NOT deploy yet (skip_deploy: true)
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Deploy to AWS
  - Merge feature branch to main
  - Monitor workflow execution
  - Verify deployment succeeds
  - Check CloudFormation stack in AWS Console
  - Verify all resources created
  - _Requirements: 8.1-8.6_

- [x] 14. Verify deployed resources
  - Check S3 bucket exists with correct name
  - Check DynamoDB table exists with GSIs
  - Check Lambda function exists with correct configuration
  - Check Vector store bucket and index exist
  - Check SSM parameters exported
  - Check CloudWatch Logs group created
  - _Requirements: 2.1-2.8, 3.1-3.7_

- [x] 15. Test Lambda function
  - Upload test document to S3 bucket with prefix "assistants/"
  - Verify Lambda triggered by S3 event
  - Check CloudWatch Logs for Lambda execution
  - Verify embeddings stored in vector store
  - Verify metadata stored in DynamoDB
  - Query vector store to verify search works
  - _Requirements: 9.1-9.14_

- [x] 16. Final verification
  - Verify existing AppApiStack resources unchanged
  - Verify existing RAG functionality still works
  - Verify new RAG stack operates independently
  - Verify no naming conflicts
  - Document any issues or observations
  - _Requirements: 21.1-21.18_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases
- The implementation reuses existing Dockerfile and Lambda code without modifications
- All new resources use "rag-" prefix to avoid conflicts with existing "assistants-" resources
- The stack can be deployed independently without affecting AppApiStack

## Success Criteria

- [ ] RagIngestionStack can be synthesized without errors
- [ ] RagIngestionStack can be deployed independently
- [ ] All resources created with correct configuration
- [ ] Lambda function can process documents successfully
- [ ] SSM parameters exported correctly
- [ ] CI/CD pipeline runs successfully
- [ ] No interference with existing AppApiStack resources
- [ ] All tests pass (unit tests and property tests)
- [ ] Documentation complete and accurate
