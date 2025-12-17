# Quota Management CDK Infrastructure

This directory contains AWS CDK infrastructure for the Quota Management System Phase 1.

## Overview

The CDK stack creates two DynamoDB tables for quota management:

### UserQuotas Table
- Stores quota tiers and assignments
- **Primary Key**: PK (HASH), SK (RANGE)
- **GSIs**:
  - `AssignmentTypeIndex` (GSI1): Query assignments by type
  - `UserAssignmentIndex` (GSI2): O(1) direct user lookup
  - `RoleAssignmentIndex` (GSI3): Query role-based assignments
- **Billing**: PAY_PER_REQUEST
- **Point-in-Time Recovery**: Enabled

### QuotaEvents Table
- Stores quota enforcement events (blocks)
- **Primary Key**: PK (HASH), SK (RANGE)
- **GSIs**:
  - `TierEventIndex` (GSI5): Analytics on tier usage
- **Billing**: PAY_PER_REQUEST
- **Point-in-Time Recovery**: Enabled

## Prerequisites

```bash
# Install Node.js dependencies
cd cdk
npm install

# Install AWS CDK CLI globally (if not already installed)
npm install -g aws-cdk

# Bootstrap CDK (first time only, per account/region)
cdk bootstrap
```

## Deployment

### Development Environment

```bash
# View changes before deployment
npm run diff:dev

# Deploy to dev
npm run deploy:dev

# OR use cdk directly
cdk deploy QuotaStack-dev
```

### Production Environment

```bash
# View changes before deployment
npm run diff:prod

# Deploy to prod
npm run deploy:prod

# OR use cdk directly
cdk deploy QuotaStack-prod --context environment=prod
```

## Verification

After deployment, verify tables were created:

```bash
# List DynamoDB tables
aws dynamodb list-tables --query "TableNames[?contains(@, 'UserQuotas')]"

# Describe UserQuotas table
aws dynamodb describe-table --table-name UserQuotas-dev \
  --query "Table.{Name:TableName, Status:TableStatus, Billing:BillingModeSummary.BillingMode, GSIs:GlobalSecondaryIndexes[].IndexName}"

# Describe QuotaEvents table
aws dynamodb describe-table --table-name QuotaEvents-dev \
  --query "Table.{Name:TableName, Status:TableStatus, Billing:BillingModeSummary.BillingMode, GSIs:GlobalSecondaryIndexes[].IndexName}"
```

Expected GSIs:
- **UserQuotas**: `["AssignmentTypeIndex", "UserAssignmentIndex", "RoleAssignmentIndex"]`
- **QuotaEvents**: `["TierEventIndex"]`

## Table Configuration

### UserQuotas-{env}

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | String | Entity identifier (e.g., `QUOTA_TIER#<tier_id>`, `ASSIGNMENT#<assignment_id>`) |
| SK | String | Metadata or sort key |
| GSI1PK | String | Assignment type key |
| GSI1SK | String | Priority sort key |
| GSI2PK | String | User identifier key |
| GSI2SK | String | Assignment sort key |
| GSI3PK | String | Role identifier key |
| GSI3SK | String | Priority sort key |

### QuotaEvents-{env}

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | String | User identifier (e.g., `USER#<user_id>`) |
| SK | String | Event timestamp key |
| GSI5PK | String | Tier identifier key |
| GSI5SK | String | Timestamp sort key |

## Cost Estimation

**Development (low usage):**
- Both tables use PAY_PER_REQUEST billing
- ~10K quota items: negligible cost
- ~1M events/month: ~$1.25/month

**Production (high usage):**
- 100K quota items: negligible cost
- 10M events/month: ~$12.50/month
- Data transfer and backups: additional cost

## Scripts

```bash
npm run build          # Compile TypeScript
npm run watch          # Watch mode
npm run cdk            # Run CDK CLI
npm run deploy:dev     # Deploy to dev
npm run deploy:prod    # Deploy to prod
npm run diff:dev       # View dev changes
npm run diff:prod      # View prod changes
npm run synth:dev      # Synthesize dev stack
npm run synth:prod     # Synthesize prod stack
npm run destroy:dev    # Destroy dev stack
npm run destroy:prod   # Destroy prod stack
```

## Clean Up

To remove the infrastructure:

```bash
# Development
npm run destroy:dev

# Production (use with caution!)
npm run destroy:prod
```

**Note**: Production tables have `RemovalPolicy.RETAIN` to prevent accidental deletion. You'll need to manually delete them after stack deletion if desired.

## Troubleshooting

### Bootstrap CDK
If you see "CDK bootstrap required" error:
```bash
cdk bootstrap aws://<account>/<region>
```

### Permissions
Ensure your AWS credentials have permissions for:
- `dynamodb:CreateTable`
- `dynamodb:DescribeTable`
- `dynamodb:TagResource`
- `cloudformation:CreateStack`
- `cloudformation:DescribeStacks`

### View CloudFormation Template
```bash
cdk synth QuotaStack-dev
```

## Integration with Backend

Update backend `.env` to use deployed tables:

```bash
# For local development, keep default names
DYNAMODB_QUOTA_TABLE=UserQuotas
DYNAMODB_EVENTS_TABLE=QuotaEvents

# For deployed environment, use suffixed names
DYNAMODB_QUOTA_TABLE=UserQuotas-dev
DYNAMODB_EVENTS_TABLE=QuotaEvents-dev
```

## Next Steps

After deployment:
1. Run backend application
2. Use admin API to create tiers: `POST /api/admin/quota/tiers`
3. Create assignments: `POST /api/admin/quota/assignments`
4. Verify quota resolution works for sample users

See `docs/QUOTA_MANAGEMENT_PHASE1_SPEC.md` for full implementation details.
