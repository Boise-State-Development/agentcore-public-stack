# RAG Ingestion Stack - Deployment Guide

## Overview

This guide provides step-by-step instructions for deploying the RAG Ingestion Stack to AWS. The implementation is complete, and all code has been written. This guide covers the remaining manual deployment and verification steps.

## Implementation Status

✅ **All implementation tasks complete (Tasks 1-11)**

The following have been implemented:
- CDK stack code (`infrastructure/lib/rag-ingestion-stack.ts`)
- Configuration management (`infrastructure/lib/config.ts`)
- CI/CD workflow (`.github/workflows/rag-ingestion.yml`)
- Shell scripts (`scripts/stack-rag-ingestion/`)
- Unit tests (`infrastructure/test/rag-ingestion-stack.test.ts`)
- Property-based tests (all 6 properties implemented)
- Stack registration in CDK app

## Prerequisites

Before deploying, ensure you have:

### 1. AWS Infrastructure
- ✅ Infrastructure Stack deployed (provides VPC and network resources)
- ✅ AWS account with appropriate permissions
- ✅ AWS CLI configured with credentials

### 2. GitHub Configuration

**GitHub Variables** (Settings → Secrets and variables → Actions → Variables):
```
CDK_PROJECT_PREFIX = bsu-agentcore (or your project prefix)
CDK_VPC_CIDR = 10.0.0.0/16 (or your VPC CIDR)
CDK_RAG_ENABLED = true
CDK_RAG_CORS_ORIGINS = https://your-frontend-domain.com
AWS_REGION = us-west-2 (or your region)
```

**GitHub Secrets** (Settings → Secrets and variables → Actions → Secrets):
```
CDK_AWS_ACCOUNT = 123456789012 (your AWS account ID)
AWS_ROLE_ARN = arn:aws:iam::123456789012:role/GitHubActionsRole (if using OIDC)
AWS_ACCESS_KEY_ID = AKIA... (your AWS access key)
AWS_SECRET_ACCESS_KEY = ... (your AWS secret key)
```

### 3. Local Environment (for testing)
```bash
# Install dependencies
cd infrastructure
npm install

cd ../backend
pip install -e .

# Set environment variables
export CDK_PROJECT_PREFIX="bsu-agentcore"
export CDK_AWS_REGION="us-west-2"
export CDK_AWS_ACCOUNT="123456789012"
export CDK_RAG_ENABLED="true"
export CDK_RAG_CORS_ORIGINS="https://your-frontend.com"
```

## Deployment Steps

### Step 1: Test CI/CD Pipeline (Task 12)

**Purpose:** Verify the workflow runs successfully without deploying to AWS.

1. **Create a feature branch:**
   ```bash
   git checkout -b feature/rag-ingestion-test
   ```

2. **Push to GitHub:**
   ```bash
   git push origin feature/rag-ingestion-test
   ```

3. **Create a Pull Request** to trigger the workflow

4. **Monitor the workflow** in GitHub Actions:
   - Go to Actions tab in GitHub
   - Watch the "RagIngestionStack.BuildTest.Deploy" workflow
   - Verify all jobs pass:
     - ✅ install
     - ✅ build-docker
     - ✅ build-cdk
     - ✅ test-docker
     - ✅ test-cdk
     - ✅ synth-cdk
     - ✅ push-to-ecr
     - ⏭️ deploy-infrastructure (skipped for PR)

5. **Expected Results:**
   - Docker image builds successfully (ARM64 architecture)
   - CDK templates synthesize without errors
   - All tests pass
   - Image pushed to ECR
   - Deployment skipped (only runs on main branch)

**Troubleshooting:**
- If build-docker fails: Check Dockerfile.rag-ingestion syntax
- If test-cdk fails: Check CloudFormation template validity
- If push-to-ecr fails: Verify AWS credentials and ECR permissions

### Step 2: Deploy to AWS (Task 13)

**Purpose:** Deploy the RAG Ingestion Stack to AWS.

1. **Merge the PR to main:**
   ```bash
   git checkout main
   git merge feature/rag-ingestion-test
   git push origin main
   ```

2. **Monitor the deployment workflow:**
   - Go to Actions tab in GitHub
   - Watch the workflow triggered by the push to main
   - This time, the deploy-infrastructure job will run

3. **Verify deployment succeeds:**
   - Check the workflow completes successfully
   - Review the deployment summary in GitHub Actions
   - Download the deployment outputs artifact

4. **Check CloudFormation in AWS Console:**
   - Go to CloudFormation service
   - Find stack: `{ProjectPrefix}-RagIngestionStack`
   - Status should be: `CREATE_COMPLETE` or `UPDATE_COMPLETE`
   - Review the Resources tab

**Alternative: Manual Deployment**

If you prefer to deploy manually:

```bash
# From project root
cd infrastructure

# Synthesize the stack
bash ../scripts/stack-rag-ingestion/synth.sh

# Deploy the stack
bash ../scripts/stack-rag-ingestion/deploy.sh
```

### Step 3: Verify Deployed Resources (Task 14)

**Purpose:** Confirm all AWS resources were created correctly.

1. **S3 Documents Bucket:**
   ```bash
   aws s3 ls | grep rag-documents
   # Expected: bsu-agentcore-rag-documents
   
   aws s3api get-bucket-versioning --bucket ${CDK_PROJECT_PREFIX}-rag-documents
   # Expected: Status: Enabled
   ```

2. **DynamoDB Assistants Table:**
   ```bash
   aws dynamodb describe-table --table-name ${CDK_PROJECT_PREFIX}-rag-assistants
   # Verify: TableStatus: ACTIVE
   # Verify: 3 Global Secondary Indexes
   ```

3. **Lambda Function:**
   ```bash
   aws lambda get-function --function-name ${CDK_PROJECT_PREFIX}-rag-ingestion
   # Verify: State: Active
   # Verify: Architecture: arm64
   # Verify: MemorySize: 10240
   # Verify: Timeout: 900
   ```

4. **S3 Vectors (if available in your region):**
   ```bash
   # Note: S3 Vectors may not be available in all regions yet
   # Check AWS Console for S3 Vectors service
   ```

5. **SSM Parameters:**
   ```bash
   aws ssm get-parameters-by-path --path /${CDK_PROJECT_PREFIX}/rag/ --recursive
   # Expected: 7 parameters
   # - documents-bucket-name
   # - documents-bucket-arn
   # - assistants-table-name
   # - assistants-table-arn
   # - vector-bucket-name
   # - vector-index-name
   # - ingestion-lambda-arn
   ```

6. **CloudWatch Logs:**
   ```bash
   aws logs describe-log-groups --log-group-name-prefix /aws/lambda/${CDK_PROJECT_PREFIX}-rag-ingestion
   # Verify log group exists
   ```

### Step 4: Test Lambda Function (Task 15)

**Purpose:** Verify the Lambda function can process documents successfully.

1. **Create a test document:**
   ```bash
   echo "This is a test document for RAG ingestion." > test-document.txt
   ```

2. **Upload to S3 with correct prefix:**
   ```bash
   # Note: Use "assistants/" prefix to trigger Lambda
   aws s3 cp test-document.txt s3://${CDK_PROJECT_PREFIX}-rag-documents/assistants/test-assistant-id/test-doc-id/test-document.txt
   ```

3. **Monitor Lambda execution:**
   ```bash
   # Wait a few seconds for Lambda to trigger
   sleep 10
   
   # Check CloudWatch Logs
   aws logs tail /aws/lambda/${CDK_PROJECT_PREFIX}-rag-ingestion --follow
   ```

4. **Verify Lambda processed the document:**
   - Check logs for successful execution
   - Look for embedding generation messages
   - Verify no errors in logs

5. **Verify embeddings stored:**
   ```bash
   # Check DynamoDB for metadata
   aws dynamodb scan --table-name ${CDK_PROJECT_PREFIX}-rag-assistants --limit 10
   # Look for items related to test-assistant-id
   ```

6. **Query vector store (if available):**
   ```bash
   # This depends on S3 Vectors API availability
   # Check AWS documentation for S3 Vectors query commands
   ```

**Expected Results:**
- Lambda invoked successfully
- Document processed without errors
- Embeddings generated using Bedrock Titan
- Metadata stored in DynamoDB
- Vectors stored in S3 Vectors

**Troubleshooting:**
- If Lambda doesn't trigger: Check S3 event notification configuration
- If Lambda fails: Check CloudWatch Logs for error details
- If Bedrock fails: Verify IAM permissions for bedrock:InvokeModel
- If vector store fails: Verify S3 Vectors permissions

### Step 5: Final Verification (Task 16)

**Purpose:** Ensure the new stack doesn't interfere with existing resources.

1. **Verify existing AppApiStack resources unchanged:**
   ```bash
   # Check existing assistants bucket (if it exists)
   aws s3 ls | grep assistants-documents
   
   # Check existing assistants table (if it exists)
   aws dynamodb describe-table --table-name ${CDK_PROJECT_PREFIX}-assistants 2>/dev/null || echo "Table doesn't exist (expected)"
   ```

2. **Verify no naming conflicts:**
   ```bash
   # List all resources with project prefix
   aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=${CDK_PROJECT_PREFIX}
   
   # Verify new resources use "rag-" prefix
   # Verify old resources use "assistants-" prefix (if they exist)
   ```

3. **Verify existing RAG functionality still works (if applicable):**
   - If you have existing RAG implementation in AppApiStack
   - Test that it still processes documents correctly
   - Verify no disruption to existing services

4. **Test independent operation:**
   - Upload document to new RAG stack
   - Verify it processes independently
   - Verify no cross-contamination with old stack

5. **Document any issues:**
   - Create a verification report
   - Note any unexpected behavior
   - Document any configuration changes needed

## Post-Deployment Configuration

### Update cdk.context.json (Optional)

Add explicit RAG configuration to `infrastructure/cdk.context.json`:

```json
{
  "ragIngestion": {
    "enabled": true,
    "corsOrigins": "https://your-frontend.com,https://your-admin.com",
    "lambdaMemorySize": 10240,
    "lambdaTimeout": 900,
    "embeddingModel": "amazon.titan-embed-text-v2",
    "vectorDimension": 1024,
    "vectorDistanceMetric": "cosine"
  }
}
```

### Configure Monitoring (Recommended)

1. **Create CloudWatch Dashboard:**
   ```bash
   # Create a dashboard for RAG metrics
   # Include: Lambda invocations, errors, duration
   # Include: DynamoDB read/write capacity
   # Include: S3 bucket requests
   ```

2. **Set up CloudWatch Alarms:**
   ```bash
   # Lambda error rate > 5%
   # Lambda duration > 10 minutes
   # DynamoDB throttling
   ```

3. **Enable X-Ray tracing (optional):**
   ```bash
   aws lambda update-function-configuration \
     --function-name ${CDK_PROJECT_PREFIX}-rag-ingestion \
     --tracing-config Mode=Active
   ```

## Rollback Procedure

If deployment fails or issues arise:

### Automatic Rollback
CloudFormation automatically rolls back on deployment failure. No action needed.

### Manual Rollback
```bash
# Delete the stack
aws cloudformation delete-stack --stack-name ${CDK_PROJECT_PREFIX}-RagIngestionStack

# Wait for deletion to complete
aws cloudformation wait stack-delete-complete --stack-name ${CDK_PROJECT_PREFIX}-RagIngestionStack

# Verify resources deleted
aws s3 ls | grep rag-documents  # Should not exist
aws dynamodb list-tables | grep rag-assistants  # Should not exist
```

### Rollback Considerations
- S3 bucket has RETAIN policy - must be deleted manually if needed
- DynamoDB table has RETAIN policy in prod - must be deleted manually
- ECR images remain - can be deleted manually if needed

## Troubleshooting Guide

### Common Issues

#### 1. Stack Synthesis Fails
**Symptom:** `cdk synth` fails with errors

**Solutions:**
- Check TypeScript compilation: `npm run build`
- Verify config.ts has all required fields
- Check SSM parameters exist (from Infrastructure Stack)

#### 2. Docker Build Fails
**Symptom:** Docker build fails in CI/CD

**Solutions:**
- Check Dockerfile.rag-ingestion syntax
- Verify base image is accessible
- Check Python dependencies in pyproject.toml

#### 3. Lambda Function Fails
**Symptom:** Lambda invocations fail with errors

**Solutions:**
- Check CloudWatch Logs for error details
- Verify environment variables are set correctly
- Check IAM permissions
- Verify Bedrock model is available in region

#### 4. S3 Event Notification Not Working
**Symptom:** Lambda doesn't trigger on S3 upload

**Solutions:**
- Verify S3 event notification is configured
- Check Lambda permission for S3 to invoke
- Verify prefix filter is "assistants/"
- Check Lambda function is active

#### 5. Vector Store Errors
**Symptom:** Vector operations fail

**Solutions:**
- Verify S3 Vectors is available in your region
- Check IAM permissions for s3vectors:* actions
- Verify vector bucket and index exist
- Check vector dimension matches Titan embeddings (1024)

## Success Criteria

✅ **Deployment Successful** when:
- CloudFormation stack status is CREATE_COMPLETE
- All resources created in AWS
- Lambda function can process documents
- Embeddings stored in vector store
- Metadata stored in DynamoDB
- SSM parameters exported correctly
- No interference with existing resources

## Next Steps

After successful deployment:

1. **Integration with AppApiStack (Future Phase):**
   - Update AppApiStack to use new RAG resources via SSM
   - Test application with new RAG stack
   - Migrate traffic to new stack
   - Remove old RAG resources from AppApiStack

2. **Performance Optimization:**
   - Monitor Lambda execution times
   - Optimize chunk size for embeddings
   - Tune Lambda memory allocation
   - Implement caching if needed

3. **Cost Optimization:**
   - Review Lambda invocation costs
   - Optimize DynamoDB capacity
   - Implement S3 lifecycle policies
   - Monitor Bedrock API costs

4. **Security Hardening:**
   - Review IAM permissions (principle of least privilege)
   - Enable VPC for Lambda (if needed)
   - Implement encryption at rest and in transit
   - Set up AWS WAF rules (if exposing API)

## Support and Documentation

- **Requirements:** `.kiro/specs/rag-ingestion-stack/requirements.md`
- **Design:** `.kiro/specs/rag-ingestion-stack/design.md`
- **Tasks:** `.kiro/specs/rag-ingestion-stack/tasks.md`
- **Verification:** `.kiro/specs/rag-ingestion-stack/task-7-verification-results.md`

For issues or questions, refer to the design document or create a GitHub issue.

---

**Last Updated:** 2025-01-27  
**Status:** Ready for Deployment  
**Version:** 1.0.0
