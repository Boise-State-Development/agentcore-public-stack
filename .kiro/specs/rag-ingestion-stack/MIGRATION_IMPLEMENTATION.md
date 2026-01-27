# RAG Ingestion Stack Migration - Implementation Complete

## Changes Made

I've successfully updated the AppApiStack to use the new RAG resources from RagIngestionStack. Here's what was changed:

### 1. Updated Environment Variables (Line ~1142)

**Changed FROM** (hardcoded local resources):
```typescript
ASSISTANTS_DOCUMENTS_BUCKET_NAME: assistantsDocumentsBucket.bucketName,
ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
ASSISTANTS_VECTOR_STORE_BUCKET_NAME: assistantsVectorStoreBucketName,
ASSISTANTS_VECTOR_STORE_INDEX_NAME: assistantsVectorIndexName,
```

**Changed TO** (imported from RagIngestionStack via SSM):
```typescript
// RAG resources - imported from RagIngestionStack via SSM
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

### 2. Added IAM Permissions (Line ~1180)

Added a new section to grant the ECS task role permissions to access the new RAG resources:

```typescript
// ============================================================
// Grant permissions for NEW RAG resources (from RagIngestionStack)
// ============================================================

// Import RAG resource identifiers from SSM
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

// Grant permissions to ECS task role for RAG resources
ragDocumentsBucket.grantReadWrite(taskDefinition.taskRole);
ragAssistantsTable.grantReadWriteData(taskDefinition.taskRole);

// Grant S3 Vectors permissions for RAG vector store
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

## What This Achieves

1. **ECS tasks now use the new RAG resources** - Environment variables point to the new S3 bucket, DynamoDB table, and vector store
2. **Proper IAM permissions** - ECS task role can read/write to the new resources
3. **No code changes in the application** - The Python code uses the same environment variable names, so no changes needed
4. **Old resources remain untouched** - The old RAG resources in AppApiStack are still defined but no longer used

## Next Steps - What You Need to Do

### Step 1: Build and Deploy

```bash
cd infrastructure
npm run build
cdk synth AppApiStack
cdk deploy AppApiStack --require-approval never
```

This will:
- Update the ECS task definition with new environment variables
- Grant IAM permissions to the new resources
- Trigger a rolling deployment of the ECS service (zero downtime)

### Step 2: Verify the Deployment

#### Check ECS Task Environment Variables

```bash
# Set your project prefix
export PROJECT_PREFIX="bsu-agentcore"  # or your actual prefix

# Get the running task ARN
TASK_ARN=$(aws ecs list-tasks \
  --cluster ${PROJECT_PREFIX}-ecs-cluster \
  --service-name ${PROJECT_PREFIX}-app-api \
  --query 'taskArns[0]' \
  --output text)

# Describe the task to see environment variables
aws ecs describe-tasks \
  --cluster ${PROJECT_PREFIX}-ecs-cluster \
  --tasks ${TASK_ARN} \
  --query 'tasks[0].containers[0].environment' \
  --output table
```

Look for these environment variables and verify they point to the new resources:
- `ASSISTANTS_DOCUMENTS_BUCKET_NAME` should be `${PROJECT_PREFIX}-rag-documents`
- `ASSISTANTS_TABLE_NAME` should be `${PROJECT_PREFIX}-rag-assistants`
- `ASSISTANTS_VECTOR_STORE_BUCKET_NAME` should be `${PROJECT_PREFIX}-rag-vector-store-v1`
- `ASSISTANTS_VECTOR_STORE_INDEX_NAME` should be `${PROJECT_PREFIX}-rag-vector-index-v1`

### Step 3: Test RAG Functionality

1. **Upload a document** via the frontend:
   - Go to the assistants section
   - Create or select an assistant
   - Upload a test document (PDF, DOCX, etc.)

2. **Monitor the new Lambda function**:
   ```bash
   # Watch Lambda logs in real-time
   aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
   ```

3. **Verify document processing**:
   ```bash
   # Check if document appears in new S3 bucket
   aws s3 ls s3://${PROJECT_PREFIX}-rag-documents/assistants/
   
   # Check if metadata appears in new DynamoDB table
   aws dynamodb scan \
     --table-name ${PROJECT_PREFIX}-rag-assistants \
     --max-items 5
   ```

4. **Test search/retrieval** in the frontend:
   - Ask the assistant a question about the uploaded document
   - Verify it can retrieve relevant information

### Step 4: Monitor for Errors

```bash
# Monitor ECS service logs
aws logs tail /ecs/${PROJECT_PREFIX}/app-api --follow

# Monitor Lambda logs
aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
```

Look for any errors related to:
- S3 access denied
- DynamoDB access denied
- S3 Vectors access denied
- Missing environment variables

### Step 5: Clean Up Old Resources (After Verification)

**⚠️ ONLY DO THIS AFTER CONFIRMING EVERYTHING WORKS FOR AT LEAST 24 HOURS!**

Once you've verified the new RAG stack works correctly and you're confident, you can remove the old RAG resource definitions from AppApiStack.

The old resources to remove from `infrastructure/lib/app-api-stack.ts`:

1. **Assistants Table** (around line 130-200)
2. **Assistants Documents Bucket** (around line 200-220)
3. **Assistants Vector Store Bucket and Index** (around line 220-260)
4. **Assistants Documents Ingestion Lambda** (around line 260-320)
5. **Lambda permissions and S3 event notifications** (around line 320-360)

After removing these, redeploy:
```bash
cdk deploy AppApiStack --require-approval never
```

The resources will be removed from CloudFormation but retained in AWS (due to RETAIN removal policy).

## Rollback Plan

If something goes wrong, you can quickly rollback:

```bash
# Revert the changes
git checkout HEAD -- infrastructure/lib/app-api-stack.ts

# Rebuild and redeploy
cd infrastructure
npm run build
cdk deploy AppApiStack --require-approval never
```

This will restore the ECS tasks to use the old RAG resources.

## Troubleshooting

### Issue: ECS tasks fail to start after deployment

**Symptoms**: Tasks keep restarting, health checks fail

**Diagnosis**:
```bash
# Check task logs
aws logs tail /ecs/${PROJECT_PREFIX}/app-api --follow
```

**Common causes**:
1. Missing SSM parameters (RagIngestionStack not deployed)
2. IAM permission errors
3. Invalid resource names

**Solution**: Check CloudWatch Logs for specific error messages

### Issue: Documents upload but don't process

**Symptoms**: Documents appear in S3 but Lambda doesn't trigger

**Diagnosis**:
```bash
# Check if Lambda exists
aws lambda get-function --function-name ${PROJECT_PREFIX}-rag-ingestion

# Check S3 event notifications
aws s3api get-bucket-notification-configuration \
  --bucket ${PROJECT_PREFIX}-rag-documents
```

**Solution**: Verify RagIngestionStack deployed successfully and S3 event notifications are configured

### Issue: Lambda processes but embeddings not stored

**Symptoms**: Lambda runs successfully but search returns no results

**Diagnosis**:
```bash
# Check Lambda logs for S3 Vectors errors
aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
```

**Common causes**:
1. S3 Vectors permissions missing
2. Vector store bucket/index doesn't exist
3. Bedrock permissions missing

**Solution**: Check IAM permissions and verify vector store resources exist

## Success Criteria

- [ ] AppApiStack deploys successfully
- [ ] ECS tasks start and pass health checks
- [ ] Environment variables point to new RAG resources
- [ ] Documents can be uploaded via frontend
- [ ] Lambda processes documents successfully
- [ ] Embeddings are stored in vector store
- [ ] Search/retrieval works in frontend
- [ ] No errors in CloudWatch Logs

## Summary

The migration is **code-complete**! The AppApiStack now imports RAG resources from RagIngestionStack via SSM parameters and has the necessary IAM permissions.

**What's left**: You need to deploy the updated AppApiStack and verify it works. The deployment is safe and can be rolled back if needed.

**No frontend changes required** - the frontend will automatically use the new resources once the App API is updated.
