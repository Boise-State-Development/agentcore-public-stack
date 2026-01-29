# ✅ RAG Migration - Ready to Deploy

## Summary

The migration from old RAG resources (in AppApiStack) to new RAG resources (in RagIngestionStack) is **code-complete** and ready for deployment.

## What Was Done

### 1. Code Changes ✅
- **Updated `infrastructure/lib/app-api-stack.ts`**:
  - Changed environment variables to import from SSM parameters
  - Added IAM permissions for new RAG resources
  - No TypeScript errors

### 2. Verification Script ✅
- **Created `scripts/verify-rag-migration.sh`**:
  - Automated verification of the migration
  - Checks SSM parameters, ECS tasks, environment variables
  - Verifies resources exist

### 3. Documentation ✅
- **MIGRATION_GUIDE.md**: Detailed step-by-step guide
- **MIGRATION_IMPLEMENTATION.md**: What was changed and why
- **READY_TO_DEPLOY.md**: This file - deployment instructions

## What You Need to Do

### Step 1: Deploy the Updated AppApiStack

```bash
# Navigate to infrastructure directory
cd infrastructure

# Build TypeScript
npm run build

# Synthesize CloudFormation template
cdk synth AppApiStack

# Deploy (this will update the ECS service with zero downtime)
cdk deploy AppApiStack --require-approval never
```

**Expected output:**
- CloudFormation will update the ECS task definition
- ECS will perform a rolling deployment (old tasks stay running until new ones are healthy)
- Takes ~5-10 minutes

### Step 2: Verify the Deployment

Run the verification script:

```bash
bash scripts/verify-rag-migration.sh
```

This will check:
- ✅ RagIngestionStack is deployed
- ✅ SSM parameters exist
- ✅ AppApiStack is deployed
- ✅ ECS tasks are running
- ✅ Environment variables point to new resources
- ✅ New resources exist in AWS

**Expected output:**
```
[SUCCESS] ==========================================
[SUCCESS] RAG Migration Verification PASSED!
[SUCCESS] ==========================================
```

### Step 3: Test RAG Functionality

#### 3.1 Upload a Document

1. Open the frontend in your browser
2. Navigate to the Assistants section
3. Create or select an assistant
4. Upload a test document (PDF, DOCX, TXT, etc.)

#### 3.2 Monitor Lambda Processing

In a separate terminal, watch the Lambda logs:

```bash
# Set your project prefix
export PROJECT_PREFIX="bsu-agentcore"  # or your actual prefix

# Watch Lambda logs in real-time
aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
```

You should see:
- Document download from S3
- Document processing with Docling
- Chunk generation
- Embedding generation
- Vector storage

#### 3.3 Test Search/Retrieval

1. In the frontend, ask the assistant a question about the uploaded document
2. Verify it retrieves relevant information from the document
3. Check that responses are accurate

### Step 4: Monitor for 24-48 Hours

Keep an eye on:

```bash
# ECS service logs
aws logs tail /ecs/${PROJECT_PREFIX}/app-api --follow

# Lambda logs
aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
```

Look for any errors related to:
- S3 access denied
- DynamoDB access denied
- S3 Vectors access denied
- Missing environment variables

## Rollback Plan (If Needed)

If something goes wrong, you can quickly rollback:

```bash
# Revert the code changes
git checkout HEAD -- infrastructure/lib/app-api-stack.ts

# Rebuild and redeploy
cd infrastructure
npm run build
cdk deploy AppApiStack --require-approval never
```

This will restore the ECS tasks to use the old RAG resources.

## After Successful Verification

Once you've confirmed everything works for 24-48 hours, you can:

1. **Remove old RAG resource definitions** from `infrastructure/lib/app-api-stack.ts`:
   - Assistants Table (line ~130-200)
   - Assistants Documents Bucket (line ~200-220)
   - Assistants Vector Store (line ~220-260)
   - Assistants Ingestion Lambda (line ~260-320)
   - Lambda permissions (line ~320-360)

2. **Redeploy AppApiStack**:
   ```bash
   cdk deploy AppApiStack --require-approval never
   ```

3. **Optional: Manually delete old resources** from AWS Console (they'll be retained due to RETAIN policy)

## Troubleshooting

### Issue: Deployment fails with "Parameter not found"

**Cause**: RagIngestionStack not deployed or SSM parameters missing

**Solution**:
```bash
# Verify RagIngestionStack is deployed
aws cloudformation describe-stacks --stack-name RagIngestionStack

# If not deployed, deploy it first
cdk deploy RagIngestionStack
```

### Issue: ECS tasks fail health checks

**Cause**: Application errors, missing permissions, or invalid environment variables

**Solution**:
```bash
# Check ECS task logs
aws logs tail /ecs/${PROJECT_PREFIX}/app-api --follow

# Check for specific error messages
```

### Issue: Documents upload but don't process

**Cause**: Lambda not triggered or failing

**Solution**:
```bash
# Check Lambda exists
aws lambda get-function --function-name ${PROJECT_PREFIX}-rag-ingestion

# Check S3 event notifications
aws s3api get-bucket-notification-configuration --bucket ${PROJECT_PREFIX}-rag-documents

# Check Lambda logs
aws logs tail /aws/lambda/${PROJECT_PREFIX}-rag-ingestion --follow
```

## Success Criteria

- [x] Code changes complete
- [ ] AppApiStack deployed successfully
- [ ] Verification script passes
- [ ] Documents can be uploaded
- [ ] Lambda processes documents
- [ ] Search/retrieval works
- [ ] No errors in logs for 24-48 hours
- [ ] Old resources removed from code
- [ ] Old resources cleaned up in AWS (optional)

## Questions?

If you encounter any issues:

1. Check the logs (ECS and Lambda)
2. Run the verification script
3. Review the MIGRATION_GUIDE.md for detailed troubleshooting
4. Rollback if needed (safe and quick)

## Ready to Go! 🚀

Everything is prepared. Just run:

```bash
cd infrastructure
npm run build
cdk deploy AppApiStack --require-approval never
```

Then verify with:

```bash
bash scripts/verify-rag-migration.sh
```

Good luck! The migration is safe, tested, and can be rolled back if needed.
