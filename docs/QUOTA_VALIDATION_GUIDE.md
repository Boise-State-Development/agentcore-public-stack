# Quota Management System - Validation Guide

**Purpose:** Step-by-step validation of the Phase 1 Quota Management implementation.

**Estimated Time:** 30-45 minutes

---

## Prerequisites Checklist

Before starting validation, ensure you have:

- [ ] AWS credentials configured (`~/.aws/credentials` or environment variables)
- [ ] Python 3.13+ installed
- [ ] Node.js 18+ installed (for CDK)
- [ ] Docker running (for local development)
- [ ] Git repository cloned and up to date

---

## Phase 1: Environment Setup (5-10 minutes)

### Step 1.1: Install Backend Dependencies

```bash
cd backend/src

# Create virtual environment if not exists
python -m venv ../venv
source ../venv/bin/activate  # On Windows: ..\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Verify quota module imports
python -c "from agents.strands_agent.quota import QuotaTier, QuotaResolver; print('✅ Quota module loaded')"
```

**Expected Output:**
```
✅ Quota module loaded
```

### Step 1.2: Install CDK Dependencies

```bash
cd ../../cdk

# Install Node dependencies
npm install

# Verify CDK CLI
npx cdk --version
```

**Expected Output:**
```
2.120.0 (or higher)
```

### Step 1.3: Configure AWS Credentials

```bash
# Verify AWS credentials
aws sts get-caller-identity
```

**Expected Output:**
```json
{
    "UserId": "...",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/youruser"
}
```

---

## Phase 2: Deploy DynamoDB Infrastructure (10-15 minutes)

### Step 2.1: Bootstrap CDK (First Time Only)

```bash
cd cdk

# Bootstrap CDK in your account/region
cdk bootstrap
```

**Expected Output:**
```
✅ Environment aws://123456789012/us-east-1 bootstrapped
```

**Note:** Only needed once per AWS account/region combination.

### Step 2.2: Review Infrastructure Changes

```bash
# View what will be created
npm run diff:dev
```

**Expected Output:**
```
Stack QuotaStack-dev
Resources
[+] AWS::DynamoDB::Table UserQuotasTable UserQuotasTable...
[+] AWS::DynamoDB::Table QuotaEventsTable QuotaEventsTable...
```

### Step 2.3: Deploy to Development

```bash
# Deploy the stack
npm run deploy:dev
```

**Expected Output:**
```
✅ QuotaStack-dev

Outputs:
QuotaStack-dev.QuotaEventsTableName = QuotaEvents-dev
QuotaStack-dev.UserQuotasTableName = UserQuotas-dev
```

**Duration:** 2-5 minutes

### Step 2.4: Verify Tables Were Created

```bash
# List DynamoDB tables
aws dynamodb list-tables --query "TableNames[?contains(@, 'Quota')]"
```

**Expected Output:**
```json
[
    "QuotaEvents-dev",
    "UserQuotas-dev"
]
```

### Step 2.5: Verify GSIs

```bash
# Check UserQuotas GSIs
aws dynamodb describe-table --table-name UserQuotas-dev \
  --query "Table.GlobalSecondaryIndexes[].IndexName"
```

**Expected Output:**
```json
[
    "AssignmentTypeIndex",
    "RoleAssignmentIndex",
    "UserAssignmentIndex"
]
```

```bash
# Check QuotaEvents GSIs
aws dynamodb describe-table --table-name QuotaEvents-dev \
  --query "Table.GlobalSecondaryIndexes[].IndexName"
```

**Expected Output:**
```json
[
    "TierEventIndex"
]
```

### Step 2.6: Verify Billing Mode

```bash
aws dynamodb describe-table --table-name UserQuotas-dev \
  --query "Table.BillingModeSummary.BillingMode"
```

**Expected Output:**
```
"PAY_PER_REQUEST"
```

✅ **Checkpoint:** DynamoDB tables deployed with correct schema and GSIs.

---

## Phase 3: Run Unit Tests (5 minutes)

### Step 3.1: Run Quota Resolver Tests

```bash
cd ../backend

# Run resolver tests
pytest tests/quota/test_resolver.py -v
```

**Expected Output:**
```
tests/quota/test_resolver.py::test_resolve_direct_user_assignment PASSED
tests/quota/test_resolver.py::test_resolve_fallback_to_role PASSED
tests/quota/test_resolver.py::test_resolve_fallback_to_default PASSED
tests/quota/test_resolver.py::test_cache_hit PASSED
tests/quota/test_resolver.py::test_no_quota_configured PASSED
tests/quota/test_resolver.py::test_cache_invalidation_specific_user PASSED
tests/quota/test_resolver.py::test_disabled_assignment_skipped PASSED

========================== 10 passed in 0.XX s ==========================
```

### Step 3.2: Run Quota Checker Tests

```bash
# Run checker tests
pytest tests/quota/test_checker.py -v
```

**Expected Output:**
```
tests/quota/test_checker.py::test_check_quota_no_quota_configured PASSED
tests/quota/test_checker.py::test_check_quota_within_limits PASSED
tests/quota/test_checker.py::test_check_quota_exceeded PASSED
tests/quota/test_checker.py::test_check_quota_unlimited_tier PASSED
tests/quota/test_checker.py::test_check_quota_daily_period PASSED
tests/quota/test_checker.py::test_check_quota_cost_aggregator_error PASSED
tests/quota/test_checker.py::test_check_quota_exactly_at_limit PASSED

========================== 9 passed in 0.XX s ==========================
```

### Step 3.3: Run All Quota Tests

```bash
# Run all quota tests together
pytest tests/quota/ -v --tb=short
```

**Expected Output:**
```
========================== 19 passed in 0.XX s ==========================
```

✅ **Checkpoint:** All unit tests passing.

---

## Phase 4: Start Backend Server (2 minutes)

### Step 4.1: Update Environment Configuration

```bash
cd backend/src

# Copy example env file if not exists
cp .env.example .env

# Edit .env to include quota table names
nano .env  # or use your preferred editor
```

**Add/Update these lines in `.env`:**
```bash
# DynamoDB Quota Tables
DYNAMODB_QUOTA_TABLE=UserQuotas-dev
DYNAMODB_EVENTS_TABLE=QuotaEvents-dev
```

### Step 4.2: Start the Backend

```bash
# Start FastAPI server
python -m uvicorn apis.app_api.main:app --reload --port 8000
```

**Expected Output:**
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

**Keep this terminal running** for the next phase.

✅ **Checkpoint:** Backend server running on port 8000.

---

## Phase 5: Validate Admin API (10-15 minutes)

### Step 5.1: Get Admin Token

You'll need a valid JWT token with admin role. For local testing, you can:

**Option A: Use existing auth flow**
```bash
# If you have Cognito/Auth0 set up, get token via login
# Store in environment variable
export ADMIN_TOKEN="your-jwt-token-here"
```

**Option B: Skip for now and test after auth setup**
```bash
# Note: Admin endpoints require authentication
# Skip to Phase 6 if auth not set up yet
```

### Step 5.2: Create a Quota Tier

```bash
curl -X POST http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "basic",
    "tierName": "Basic Tier",
    "description": "Default tier for all users",
    "monthlyCostLimit": 100.0,
    "dailyCostLimit": 5.0,
    "periodType": "monthly",
    "actionOnLimit": "block",
    "enabled": true
  }' | jq
```

**Expected Output:**
```json
{
  "tierId": "basic",
  "tierName": "Basic Tier",
  "description": "Default tier for all users",
  "monthlyCostLimit": 100.0,
  "dailyCostLimit": 5.0,
  "periodType": "monthly",
  "actionOnLimit": "block",
  "enabled": true,
  "createdAt": "2025-12-17T...",
  "updatedAt": "2025-12-17T...",
  "createdBy": "admin_user_id"
}
```

### Step 5.3: Create Additional Tiers

```bash
# Premium Tier
curl -X POST http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "premium",
    "tierName": "Premium Tier",
    "description": "For premium users",
    "monthlyCostLimit": 500.0,
    "dailyCostLimit": 20.0,
    "periodType": "monthly",
    "enabled": true
  }' | jq

# Enterprise Tier
curl -X POST http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "enterprise",
    "tierName": "Enterprise Tier",
    "description": "For enterprise customers",
    "monthlyCostLimit": 2000.0,
    "dailyCostLimit": 100.0,
    "periodType": "monthly",
    "enabled": true
  }' | jq
```

### Step 5.4: List All Tiers

```bash
curl http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq
```

**Expected Output:**
```json
[
  {
    "tierId": "basic",
    "tierName": "Basic Tier",
    ...
  },
  {
    "tierId": "premium",
    "tierName": "Premium Tier",
    ...
  },
  {
    "tierId": "enterprise",
    "tierName": "Enterprise Tier",
    ...
  }
]
```

### Step 5.5: Create Default Tier Assignment

```bash
curl -X POST http://localhost:8000/api/admin/quota/assignments \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "basic",
    "assignmentType": "default_tier",
    "priority": 100,
    "enabled": true
  }' | jq
```

**Expected Output:**
```json
{
  "assignmentId": "generated-uuid",
  "tierId": "basic",
  "assignmentType": "default_tier",
  "priority": 100,
  "enabled": true,
  "createdAt": "2025-12-17T...",
  ...
}
```

### Step 5.6: Create Role-Based Assignment

```bash
curl -X POST http://localhost:8000/api/admin/quota/assignments \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "premium",
    "assignmentType": "jwt_role",
    "jwtRole": "Faculty",
    "priority": 200,
    "enabled": true
  }' | jq
```

### Step 5.7: Create Direct User Assignment

```bash
curl -X POST http://localhost:8000/api/admin/quota/assignments \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "enterprise",
    "assignmentType": "direct_user",
    "userId": "test_user_123",
    "priority": 300,
    "enabled": true
  }' | jq
```

### Step 5.8: List All Assignments

```bash
curl http://localhost:8000/api/admin/quota/assignments \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq
```

**Expected Output:**
```json
[
  {
    "assignmentId": "...",
    "tierId": "basic",
    "assignmentType": "default_tier",
    ...
  },
  {
    "assignmentId": "...",
    "tierId": "premium",
    "assignmentType": "jwt_role",
    "jwtRole": "Faculty",
    ...
  },
  {
    "assignmentId": "...",
    "tierId": "enterprise",
    "assignmentType": "direct_user",
    "userId": "test_user_123",
    ...
  }
]
```

✅ **Checkpoint:** Tiers and assignments created successfully via Admin API.

---

## Phase 6: Verify DynamoDB Data (5 minutes)

### Step 6.1: Check Tiers in DynamoDB

```bash
aws dynamodb query \
  --table-name UserQuotas-dev \
  --key-condition-expression "begins_with(PK, :prefix)" \
  --expression-attribute-values '{":prefix":{"S":"QUOTA_TIER#"}}' \
  --query "Items[].{TierId:tierId.S, Name:tierName.S, Limit:monthlyCostLimit.N}"
```

**Expected Output:**
```json
[
  {
    "TierId": "basic",
    "Name": "Basic Tier",
    "Limit": "100.0"
  },
  {
    "TierId": "premium",
    "Name": "Premium Tier",
    "Limit": "500.0"
  },
  {
    "TierId": "enterprise",
    "Name": "Enterprise Tier",
    "Limit": "2000.0"
  }
]
```

### Step 6.2: Check Assignments via GSI

```bash
# Query default tier assignments via GSI1
aws dynamodb query \
  --table-name UserQuotas-dev \
  --index-name AssignmentTypeIndex \
  --key-condition-expression "GSI1PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"ASSIGNMENT_TYPE#default_tier"}}' \
  --query "Items[].{AssignmentId:assignmentId.S, TierId:tierId.S, Priority:priority.N}"
```

**Expected Output:**
```json
[
  {
    "AssignmentId": "...",
    "TierId": "basic",
    "Priority": "100"
  }
]
```

### Step 6.3: Verify User Assignment GSI

```bash
# Query direct user assignment via GSI2
aws dynamodb query \
  --table-name UserQuotas-dev \
  --index-name UserAssignmentIndex \
  --key-condition-expression "GSI2PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"USER#test_user_123"}}' \
  --query "Items[].{UserId:userId.S, TierId:tierId.S}"
```

**Expected Output:**
```json
[
  {
    "UserId": "test_user_123",
    "TierId": "enterprise"
  }
]
```

### Step 6.4: Verify Role Assignment GSI

```bash
# Query role assignment via GSI3
aws dynamodb query \
  --table-name UserQuotas-dev \
  --index-name RoleAssignmentIndex \
  --key-condition-expression "GSI3PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"ROLE#Faculty"}}' \
  --query "Items[].{Role:jwtRole.S, TierId:tierId.S}"
```

**Expected Output:**
```json
[
  {
    "Role": "Faculty",
    "TierId": "premium"
  }
]
```

✅ **Checkpoint:** Data correctly stored in DynamoDB with GSI keys.

---

## Phase 7: Validate Quota Resolution (5 minutes)

### Step 7.1: Test in Python Console

```bash
cd backend/src

# Start Python console
python
```

**Run this code:**
```python
import asyncio
from apis.shared.auth.models import User
from agents.strands_agent.quota.repository import QuotaRepository
from agents.strands_agent.quota.resolver import QuotaResolver

# Create repository and resolver
repo = QuotaRepository(
    table_name="UserQuotas-dev",
    events_table_name="QuotaEvents-dev"
)
resolver = QuotaResolver(repository=repo, cache_ttl_seconds=300)

# Test 1: Direct user assignment
user1 = User(
    user_id="test_user_123",
    email="test@example.com",
    name="Test User",
    roles=[]
)

resolved1 = asyncio.run(resolver.resolve_user_quota(user1))
print(f"✅ User 1 resolved: {resolved1.tier.tier_name} (matched by: {resolved1.matched_by})")
# Expected: "Enterprise Tier (matched by: direct_user)"

# Test 2: Role-based assignment
user2 = User(
    user_id="faculty_user",
    email="faculty@example.com",
    name="Faculty User",
    roles=["Faculty"]
)

resolved2 = asyncio.run(resolver.resolve_user_quota(user2))
print(f"✅ User 2 resolved: {resolved2.tier.tier_name} (matched by: {resolved2.matched_by})")
# Expected: "Premium Tier (matched by: jwt_role:Faculty)"

# Test 3: Default tier fallback
user3 = User(
    user_id="random_user",
    email="random@example.com",
    name="Random User",
    roles=[]
)

resolved3 = asyncio.run(resolver.resolve_user_quota(user3))
print(f"✅ User 3 resolved: {resolved3.tier.tier_name} (matched by: {resolved3.matched_by})")
# Expected: "Basic Tier (matched by: default_tier)"

# Test 4: Cache hit
resolved3_cached = asyncio.run(resolver.resolve_user_quota(user3))
print(f"✅ User 3 cached: {resolved3_cached.tier.tier_name} (same object: {resolved3.tier is resolved3_cached.tier})")
# Expected: True (cache hit)

print("\n✅ All quota resolution tests passed!")
```

**Expected Output:**
```
✅ User 1 resolved: Enterprise Tier (matched by: direct_user)
✅ User 2 resolved: Premium Tier (matched by: jwt_role:Faculty)
✅ User 3 resolved: Basic Tier (matched by: default_tier)
✅ User 3 cached: Basic Tier (same object: True)

✅ All quota resolution tests passed!
```

✅ **Checkpoint:** Quota resolution working correctly with priority ordering.

---

## Phase 8: Validate Quota Checker (Optional, 5 minutes)

**Note:** This requires the cost tracking system to be set up. Skip if not available.

```python
from agents.strands_agent.quota.checker import QuotaChecker
from agents.strands_agent.quota.event_recorder import QuotaEventRecorder
from apis.app_api.costs.aggregator import CostAggregator

# Create checker
event_recorder = QuotaEventRecorder(repository=repo)
cost_aggregator = CostAggregator()
checker = QuotaChecker(
    resolver=resolver,
    cost_aggregator=cost_aggregator,
    event_recorder=event_recorder
)

# Check quota for user
result = asyncio.run(checker.check_quota(user1))
print(f"Allowed: {result.allowed}")
print(f"Message: {result.message}")
print(f"Current Usage: ${result.current_usage:.2f}")
print(f"Quota Limit: ${result.quota_limit:.2f}")
print(f"Percentage Used: {result.percentage_used:.1f}%")
```

---

## Phase 9: Check CloudWatch Metrics (Optional, 5 minutes)

### Step 9.1: Verify No Table Scans

```bash
# Check for Scan operations (should be 0)
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=UserQuotas-dev Name=Operation,Value=Scan \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --statistics Sum \
  --query 'Datapoints[].Sum'
```

**Expected Output:**
```json
[]
```
(Empty array = no scans)

### Step 9.2: Check Query Operations

```bash
# Check for Query operations (should have some)
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=UserQuotas-dev Name=Operation,Value=Query \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --statistics Sum
```

**Expected:** Some non-zero values indicating successful queries.

---

## Phase 10: Cleanup (Optional)

### Step 10.1: Delete Test Data

```bash
# Delete assignments (get IDs first)
ASSIGNMENT_IDS=$(aws dynamodb query \
  --table-name UserQuotas-dev \
  --index-name AssignmentTypeIndex \
  --key-condition-expression "begins_with(GSI1PK, :prefix)" \
  --expression-attribute-values '{":prefix":{"S":"ASSIGNMENT_TYPE#"}}' \
  --query "Items[].assignmentId.S" \
  --output text)

# Delete each assignment via API
for id in $ASSIGNMENT_IDS; do
  curl -X DELETE "http://localhost:8000/api/admin/quota/assignments/$id" \
    -H "Authorization: Bearer $ADMIN_TOKEN"
done

# Delete tiers via API
curl -X DELETE http://localhost:8000/api/admin/quota/tiers/basic \
  -H "Authorization: Bearer $ADMIN_TOKEN"
curl -X DELETE http://localhost:8000/api/admin/quota/tiers/premium \
  -H "Authorization: Bearer $ADMIN_TOKEN"
curl -X DELETE http://localhost:8000/api/admin/quota/tiers/enterprise \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Step 10.2: Destroy CDK Stack (Caution!)

```bash
cd cdk

# CAUTION: This will delete all DynamoDB tables and data!
npm run destroy:dev
```

---

## Validation Checklist

Mark each item as you complete it:

### Infrastructure
- [ ] CDK dependencies installed
- [ ] AWS credentials configured
- [ ] DynamoDB tables deployed (UserQuotas, QuotaEvents)
- [ ] All GSIs created (3 for UserQuotas, 1 for QuotaEvents)
- [ ] Tables using PAY_PER_REQUEST billing

### Code Quality
- [ ] All 10 resolver tests passing
- [ ] All 9 checker tests passing
- [ ] No import errors in quota module
- [ ] Backend server starts without errors

### Admin API
- [ ] Can create quota tiers
- [ ] Can list all tiers
- [ ] Can create default tier assignment
- [ ] Can create role-based assignment
- [ ] Can create direct user assignment
- [ ] Can list all assignments

### Data Integrity
- [ ] Tiers stored correctly in DynamoDB
- [ ] Assignments have correct GSI keys
- [ ] GSI queries return expected results
- [ ] No table scans in CloudWatch metrics

### Business Logic
- [ ] Direct user assignment takes priority
- [ ] Role-based assignment works as fallback
- [ ] Default tier assignment works as final fallback
- [ ] Cache reduces database queries
- [ ] Resolver returns correct matched_by value

---

## Troubleshooting

### Issue: CDK Deploy Fails

**Error:** "CDK bootstrap required"
```bash
cdk bootstrap aws://<account>/<region>
```

### Issue: Permission Denied

**Error:** "User is not authorized to perform: dynamodb:CreateTable"
- Check IAM permissions
- Ensure user has DynamoDB and CloudFormation permissions

### Issue: Module Import Errors

**Error:** "ModuleNotFoundError: No module named 'agentcore'"
```bash
# Ensure you're in the backend/src directory
cd backend/src
export PYTHONPATH=$PWD:$PYTHONPATH
```

### Issue: Admin API 401/403

**Error:** "Not authenticated" or "Insufficient permissions"
- Verify JWT token is valid
- Check token includes admin role
- Test auth endpoint first: `curl http://localhost:8000/health`

### Issue: Table Already Exists

**Error:** "Table already exists"
- Either use existing table or delete via Console
- Or change environment name in CDK context

---

## Success Criteria

Your implementation is validated when:

1. ✅ All 19 unit tests pass
2. ✅ DynamoDB tables deployed with correct GSIs
3. ✅ Admin API CRUD operations work
4. ✅ Quota resolution returns correct tiers with priority ordering
5. ✅ Cache reduces database queries (verify via logs)
6. ✅ No table scans in CloudWatch metrics
7. ✅ GSI queries return data in expected format

---

## Next Steps

After successful validation:

1. **Integrate with Chat API**: Add quota checker to message processing middleware
2. **Set Up Monitoring**: Create CloudWatch dashboards for quota metrics
3. **Populate Production Data**: Create real tiers and assignments for your users
4. **Test Cost Tracking**: Verify cost aggregator integration
5. **Plan Phase 2**: Review `QUOTA_MANAGEMENT_PHASE2_SPEC.md` for next features

---

**Questions or Issues?**
- Check `docs/QUOTA_MANAGEMENT_IMPLEMENTATION.md` for detailed reference
- Review `docs/QUOTA_MANAGEMENT_PHASE1_SPEC.md` for specification details
- Check backend logs in `agentcore.log`

**Congratulations!** You've successfully validated the Phase 1 Quota Management implementation.
