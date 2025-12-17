# Quota Management - Quick Start Guide

**Get up and running with Phase 1 Quota Management in 10 minutes.**

---

## TL;DR - Fast Track

```bash
# 1. Deploy DynamoDB tables
cd cdk
npm install
npm run deploy:dev

# 2. Run tests
cd ../backend
pytest tests/quota/ -v

# 3. Start backend
cd src
python -m uvicorn apis.app_api.main:app --reload --port 8000

# 4. Create test data (needs admin token)
curl -X POST http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tierId":"basic","tierName":"Basic","monthlyCostLimit":100,"enabled":true}'
```

---

## What Was Implemented

### Phase 1 Scope ✅

1. **DynamoDB Tables**
   - `UserQuotas` table with 3 GSIs for fast lookups
   - `QuotaEvents` table for tracking block events
   - PAY_PER_REQUEST billing for cost optimization

2. **Backend Services**
   - `QuotaResolver` - Resolves user quotas with 5-min cache
   - `QuotaChecker` - Enforces hard limits (blocks requests)
   - `QuotaRepository` - DynamoDB access (ZERO table scans)
   - `QuotaEventRecorder` - Tracks quota violations

3. **Admin API**
   - `/api/admin/quota/tiers` - Manage quota tiers
   - `/api/admin/quota/assignments` - Manage user/role assignments
   - `/api/admin/quota/users/{id}` - Inspect user quota status

4. **CDK Infrastructure**
   - TypeScript CDK stack for DynamoDB
   - Dev/prod environment support
   - Automated deployment scripts

5. **Testing**
   - 19 unit tests (10 resolver + 9 checker)
   - Mock-based testing with pytest
   - Comprehensive coverage

---

## Key Features

### Quota Resolution Priority

1. **Direct User Assignment** (priority ~300) - Highest
2. **JWT Role Assignment** (priority ~200) - Medium
3. **Default Tier** (priority ~100) - Fallback

### Performance

- **Cache Hit**: <5ms resolution
- **Cache Miss**: 50-200ms (2-6 DynamoDB queries)
- **Cache TTL**: 5 minutes
- **Expected Hit Rate**: 90%

### Database Efficiency

- **Zero Table Scans** - All queries use primary keys or GSIs
- **Targeted Lookups** - O(1) for user, O(log n) for role
- **Pay-per-request** - Only pay for what you use

---

## File Structure

### Backend
```
backend/src/
├── agentcore/quota/              # Core logic
│   ├── models.py                 # 127 lines
│   ├── repository.py             # 455 lines
│   ├── resolver.py               # 128 lines
│   ├── checker.py                # 128 lines
│   └── event_recorder.py         # 47 lines
│
└── apis/app_api/admin/quota/     # Admin API
    ├── models.py                 # 91 lines
    ├── service.py                # 333 lines
    └── routes.py                 # 431 lines
```

### CDK
```
cdk/
├── lib/stacks/quota-stack.ts     # 152 lines
├── bin/quota-app.ts              # 34 lines
└── cdk.json                      # 50 lines
```

### Tests
```
backend/tests/quota/
├── test_resolver.py              # 10 tests
└── test_checker.py               # 9 tests
```

---

## API Examples

### Create a Tier

```bash
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
  }'
```

### Create Role Assignment

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
  }'
```

### Check User Quota

```bash
curl http://localhost:8000/api/admin/quota/users/user123?email=test@example.com&roles=Faculty \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Validation Steps

### 1. Verify Tables Exist

```bash
aws dynamodb list-tables --query "TableNames[?contains(@, 'Quota')]"
```

Expected: `["QuotaEvents-dev", "UserQuotas-dev"]`

### 2. Run Tests

```bash
cd backend
pytest tests/quota/ -v
```

Expected: `19 passed`

### 3. Test Quota Resolution

```python
from agentcore.quota.repository import QuotaRepository
from agentcore.quota.resolver import QuotaResolver
from apis.shared.auth.models import User
import asyncio

repo = QuotaRepository(table_name="UserQuotas-dev")
resolver = QuotaResolver(repository=repo)

user = User(user_id="test", email="test@example.com", name="Test", roles=[])
resolved = asyncio.run(resolver.resolve_user_quota(user))
print(f"Tier: {resolved.tier.tier_name if resolved else 'None'}")
```

### 4. Verify No Table Scans

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=UserQuotas-dev Name=Operation,Value=Scan \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --statistics Sum
```

Expected: Empty array (no scans)

---

## Common Commands

```bash
# Deploy infrastructure
cd cdk && npm run deploy:dev

# View infrastructure changes
cd cdk && npm run diff:dev

# Destroy infrastructure (CAUTION!)
cd cdk && npm run destroy:dev

# Run all tests
cd backend && pytest tests/quota/ -v

# Start backend
cd backend/src && python -m uvicorn apis.app_api.main:app --reload

# Check table status
aws dynamodb describe-table --table-name UserQuotas-dev

# List all tiers
curl http://localhost:8000/api/admin/quota/tiers -H "Authorization: Bearer $TOKEN"
```

---

## What's NOT Included (Phase 2)

- ❌ Soft limit warnings (80%, 90%)
- ❌ Quota overrides (temporary adjustments)
- ❌ Email domain matching
- ❌ Frontend UI
- ❌ Enhanced analytics
- ❌ Notification system

See `QUOTA_MANAGEMENT_PHASE2_SPEC.md` for Phase 2 features.

---

## Documentation

- **Full Spec**: `docs/QUOTA_MANAGEMENT_PHASE1_SPEC.md` (1,912 lines)
- **Implementation**: `docs/QUOTA_MANAGEMENT_IMPLEMENTATION.md` (full details)
- **Validation Guide**: `docs/QUOTA_VALIDATION_GUIDE.md` (step-by-step)
- **This File**: `docs/QUOTA_QUICK_START.md` (quick reference)

---

## Troubleshooting

### CDK Bootstrap Required
```bash
cdk bootstrap aws://<account>/<region>
```

### Module Import Error
```bash
cd backend/src
export PYTHONPATH=$PWD:$PYTHONPATH
```

### Permission Denied
- Check AWS credentials: `aws sts get-caller-identity`
- Verify IAM permissions for DynamoDB and CloudFormation

### Admin API 403
- Verify JWT token includes admin role
- Check token expiration

---

## Cost Estimate

**Development:**
- DynamoDB: ~$0.05/month
- CloudWatch: Free tier
- **Total: <$0.10/month**

**Production (100K users, 10M events/month):**
- DynamoDB: ~$4/month
- Storage: ~$0.03/month
- **Total: ~$4/month**

---

## Success Checklist

- [ ] DynamoDB tables deployed with GSIs
- [ ] All 19 unit tests passing
- [ ] Admin API CRUD operations work
- [ ] Quota resolution working with priority
- [ ] No table scans in CloudWatch
- [ ] Cache reduces database queries

---

## Next Steps

1. ✅ Deploy to dev environment
2. ✅ Run validation tests
3. ✅ Create initial tiers (basic, premium, enterprise)
4. ✅ Create default tier assignment
5. 🚀 Integrate QuotaChecker into chat middleware
6. 📊 Set up CloudWatch dashboards
7. 📋 Plan Phase 2 features

---

**Ready to deploy?** Follow the validation guide for step-by-step instructions.

**Questions?** Check the full implementation docs or raise an issue.
