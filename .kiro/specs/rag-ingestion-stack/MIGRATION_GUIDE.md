# RAG Ingestion Stack Migration Guide

## Overview

This guide explains how to switch the App API and Frontend from using the **old RAG resources** (in AppApiStack) to the **new RAG resources** (in RagIngestionStack).

## Current Architecture

### Old RAG Resources (AppApiStack)
- **S3 Bucket**: `${projectPrefix}-assistants-documents`
- **DynamoDB Table**: `${projectPrefix}-assistants`
- **Vector Store Bucket**: `${projectPrefix}-assistants-vector-store-v1`
- **Vector Store Index**: `${projectPrefix}-assistants-vector-index-v1`
- **Lambda Function**: `AssistantsDocumentsIngestionlambdaFunction`

### New RAG Resources (RagIngestionStack)
- **S3 Bucket**: `${projectPrefix}-rag-documents`
- **DynamoDB Table**: `${projectPrefix}-rag-assistants`
- **Vector Store Bucket**: `${projectPrefix}-rag-vector-store-v1`
- **Vector Store Index**: `${projectPrefix}-rag-vector-index-v1`
- **Lambda Function**: `RagIngestionLambda`

## What Needs to Change

The App API (ECS Fargate service) uses these environment variables that point to the old resources:

```typescript
// Current (OLD) - Hardcoded to AppApiStack resources
ASSISTANTS_DOCUMENTS_BUCKET_NAME: assistantsDocumentsBucket.bucketName,
ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
ASSISTANTS_VECTOR_STORE_BUCKET_NAME: assistantsVectorStoreBucketName,
ASSISTANTS_VECTOR_STORE_INDEX_NAME: assistantsVectorIndexName,
```

These need to be changed to import from SSM parameters exported by RagIngestionStack.

## Migration Steps

### Step 1: Update AppApiStack to Import RAG Resources from SSM

**File**: `infrastructure/lib/app-api-stack.ts`

**Location**: Around line 1140 in the ECS task definition environment variables

**Change FROM:**
```typescript
ASSISTANTS_DOCUMENTS_BUCKET_NAME: assistantsDocumentsBucket.bucketName,
ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
ASSISTANTS_VECTOR_STORE_BUCKET_NAME: assistantsVectorStoreBucketName,
ASSISTANTS_VECTOR_STORE_INDEX_NAME: assistantsVectorIndexName,
```

**Change TO:**
```typescript
// Import RAG resource names from RagIngestionStack via SSM
ASSISTANTS_DOCUMENTS_BUCKET_NAME: ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/documents-bucket-name`
),
ASSISTANTS_TABLE_NAME: ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/assistants-table-name`
),
ASSISTANTS_VECTOR_STORE_BUCKET_NAME: ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/vector-bucket-name`
),
ASSISTANTS_VECTOR_STORE_INDEX_NAME: ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/vector-index-name`
),
```

### Step 2: Update IAM Permissions for ECS Task

The ECS task role needs permissions to access the **new** RAG resources.

**File**: `infrastructure/lib/app-api-stack.ts`

**Location**: After the task definition, around line 1180

**Add these permission grants:**

```typescript
// Import new RAG resources for permission grants
const ragDocumentsBucketName = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/documents-bucket-name`
);
const ragDocumentsBucketArn = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/documents-bucket-arn`
);
const ragAssistantsTableName = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/assistants-table-name`
);
const ragAssistantsTableArn = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/assistants-table-arn`
);
const ragVectorBucketName = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/rag/vector-bucket-name`
);

// Import S3 bucket for permissions
const ragDocumentsBucket = s3.Bucket.fromBucketAttributes(this, "ImportedRagDocumentsBucket", {
  bucketName: ragDocumentsBucketName,
  bucketArn: ragDocumentsBucketArn,
});

// Import DynamoDB table for permissions
const ragAssistantsTable = dynamodb.Table.fromTableAttributes(this, "ImportedRagAssistantsTable", {
  tableName: ragAssistantsTableName,
  tableArn: ragAssistantsTableArn,
});

// Grant permissions to ECS task role
ragDocumentsBucket.grantReadWrite(taskDefinition.taskRole);
ragAssistantsTable.grantReadWriteData(taskDefinition.taskRole);

// Grant S3 Vectors permissions
taskDefinition.taskRole.addToPrincipalPolicy(
  new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: [
      "s3vectors:ListVectorBuckets",
      "s3vectors:GetVectorBucket",
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:ListVectors",
      "s3vectors:ListIndexes",
      "s3vectors:GetVector",
      "s3vectors:GetVectors",
      "s3vectors:DeleteVector",
    ],
    resources: [
      `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${ragVectorBucketName}`,
      `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${ragVectorBucketName}/index/*`,
    ],
  })
);
```

### Step 3: Deploy Updated AppApiStack

```bash
# Synthesize the updated stack
cd infrastructure
npm run build
cdk synth AppApiStack

# Deploy the updated stack
cdk deploy AppApiStack --require-approval never
```

This will:
1. Update the ECS task definition with new environment variables
2. Grant permissions to access new RAG resources
3. Trigger a rolling deployment of the ECS service

### Step 4: Verify the Migration

#### 4.1 Check ECS Task Environment Variables

```bash
# Get the task ARN
aws ecs list-tasks --cluster ${PROJECT_PREFIX}-ecs-cluster --service-name ${PROJECT_PREFIX}-app-api

# Describe the task to see environment variables
aws ecs describe-tasks --cluster ${PROJECT_PREFIX}-ecs-cluster --tasks <TASK_ARN>
```

Verify the environment variables point to the new resources:
- `ASSISTANTS_DOCUMENTS_BUCKET_NAME` should be `${projectPrefix}-rag-documents`
- `ASSISTANTS_TABLE_NAME` should be `${projectPrefix}-rag-assistants`

#### 4.2 Test RAG Functionality

1. **Upload a document** via the frontend
2. **Check CloudWatch Logs** for the new Lambda function:
   ```bash
   aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
   ```
3. **Verify document processing**:
   - Document appears in S3 bucket
   - Metadata appears in DynamoDB table
   - Embeddings stored in vector store
4. **Test search/retrieval** in the frontend

#### 4.3 Monitor for Errors

```bash
# Check ECS service logs
aws logs tail /ecs/${PROJECT_PREFIX}/app-api --follow

# Check Lambda logs
aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
```

### Step 5: Clean Up Old RAG Resources (After Verification)

**⚠️ ONLY DO THIS AFTER CONFIRMING EVERYTHING WORKS!**

Once you've verified the new RAG stack works correctly, you can remove the old resources from AppApiStack.

**File**: `infrastructure/lib/app-api-stack.ts`

**Remove these sections:**

1. **Assistants Table** (around line 130-200)
2. **Assistants Documents Bucket** (around line 200-220)
3. **Assistants Vector Store Bucket and Index** (around line 220-260)
4. **Assistants Documents Ingestion Lambda** (around line 260-320)
5. **Lambda permissions and S3 event notifications** (around line 320-360)

**Then redeploy:**
```bash
cdk deploy AppApiStack --require-approval never
```

This will remove the old resources from CloudFormation, but they'll be retained in AWS (due to `RETAIN` removal policy).

### Step 6: Manual Cleanup (Optional)

If you want to completely remove the old resources:

```bash
# Delete old S3 bucket (must be empty first)
aws s3 rm s3://${PROJECT_PREFIX}-assistants-documents --recursive
aws s3 rb s3://${PROJECT_PREFIX}-assistants-documents

# Delete old DynamoDB table
aws dynamodb delete-table --table-name ${PROJECT_PREFIX}-assistants

# Delete old Lambda function
aws lambda delete-function --function-name ${PROJECT_PREFIX}-assistants-documents-ingestion

# Delete old Vector Store (if needed)
# Note: S3 Vectors may require special cleanup commands
```

## Rollback Plan

If something goes wrong, you can quickly rollback:

### Option 1: Revert AppApiStack Changes

```bash
# Revert the code changes in app-api-stack.ts
git checkout HEAD -- infrastructure/lib/app-api-stack.ts

# Redeploy
cdk deploy AppApiStack --require-approval never
```

### Option 2: Use Old Resources Temporarily

The old resources still exist, so you can temporarily point back to them by reverting the environment variable changes.

## Frontend Changes

**The frontend doesn't need any changes!**

The frontend talks to the App API via REST endpoints. As long as the App API is configured correctly (Step 1-3 above), the frontend will automatically use the new RAG resources.

## Summary Checklist

- [ ] Step 1: Update AppApiStack environment variables to import from SSM
- [ ] Step 2: Add IAM permissions for new RAG resources
- [ ] Step 3: Deploy updated AppApiStack
- [ ] Step 4: Verify ECS task environment variables
- [ ] Step 4: Test document upload and processing
- [ ] Step 4: Test search/retrieval functionality
- [ ] Step 4: Monitor logs for errors
- [ ] Step 5: Remove old RAG resources from AppApiStack (after verification)
- [ ] Step 6: Manual cleanup of old AWS resources (optional)

## Troubleshooting

### Issue: ECS tasks fail to start

**Cause**: Missing IAM permissions for new resources

**Solution**: Check CloudWatch Logs for permission errors, add missing permissions to task role

### Issue: Documents not processing

**Cause**: Lambda not triggered or failing

**Solution**: 
1. Check S3 event notifications are configured
2. Check Lambda CloudWatch Logs for errors
3. Verify Lambda has permissions to access resources

### Issue: Search returns no results

**Cause**: Embeddings not stored in vector store

**Solution**:
1. Check Lambda logs for embedding generation errors
2. Verify vector store bucket and index exist
3. Check IAM permissions for S3 Vectors operations

## Support

If you encounter issues during migration, check:
1. CloudWatch Logs for ECS tasks and Lambda functions
2. CloudFormation stack events for deployment errors
3. IAM permissions for missing access rights
