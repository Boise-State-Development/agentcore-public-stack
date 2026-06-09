/**
 * Detailed DynamoDB table property tests.
 *
 * Verifies GSI configurations, TTL attributes, encryption settings,
 * and billing modes for every shared table.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

import { AuthTablesConstruct } from '../lib/constructs/data/auth-tables-construct';
import { QuotaTablesConstruct } from '../lib/constructs/data/quota-tables-construct';
import { CostTrackingTablesConstruct } from '../lib/constructs/data/cost-tracking-tables-construct';
import { AdminTablesConstruct } from '../lib/constructs/data/admin-tables-construct';
import { FileUploadConstruct } from '../lib/constructs/data/file-upload-construct';
import { SharedConversationsConstruct } from '../lib/constructs/data/shared-conversations-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { FineTuningDataConstruct } from '../lib/constructs/fine-tuning/fine-tuning-data-construct';
import { VoiceTicketConstruct } from '../lib/constructs/identity/voice-ticket-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

const config = createMockConfig();

describe('AuthTablesConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new AuthTablesConstruct(stack, 'AT', { config });
    t = Template.fromStack(stack);
  });

  it('OidcState table has TTL on expiresAt', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-oidc-state',
      TimeToLiveSpecification: { AttributeName: 'expiresAt', Enabled: true },
    });
  });

  it('BFFSessions table has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-bff-sessions',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('BFFSessions table has point-in-time recovery', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-bff-sessions',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  it('Users table has 4 GSIs', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-users',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'UserIdIndex' }),
        Match.objectLike({ IndexName: 'EmailIndex' }),
        Match.objectLike({ IndexName: 'EmailDomainIndex' }),
        Match.objectLike({ IndexName: 'StatusLoginIndex' }),
      ]),
    });
  });

  it('AppRoles table has 4 GSIs incl. SkillOwnerIndex', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-app-roles',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'JwtRoleMappingIndex' }),
        Match.objectLike({ IndexName: 'ToolRoleMappingIndex' }),
        Match.objectLike({ IndexName: 'ModelRoleMappingIndex' }),
        Match.objectLike({ IndexName: 'SkillOwnerIndex' }),
      ]),
    });
  });

  it('SkillOwnerIndex is keyed on GSI4PK/GSI4SK with full projection', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-app-roles',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'SkillOwnerIndex',
          KeySchema: [
            { AttributeName: 'GSI4PK', KeyType: 'HASH' },
            { AttributeName: 'GSI4SK', KeyType: 'RANGE' },
          ],
          Projection: { ProjectionType: 'ALL' },
        }),
      ]),
    });
  });

  it('ApiKeys table has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-api-keys',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('ApiKeys table has KeyHashIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-api-keys',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'KeyHashIndex' }),
      ]),
    });
  });

  it('all tables use PAY_PER_REQUEST billing', () => {
    const tables = t.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.BillingMode).toBe('PAY_PER_REQUEST');
    }
  });

  it('all tables use AWS_MANAGED encryption', () => {
    const tables = t.findResources('AWS::DynamoDB::Table');
    // AWS_MANAGED encryption: CDK sets SSEEnabled=true with no KMSMasterKeyId
    for (const [, resource] of Object.entries(tables)) {
      const enc = (resource as any).Properties.SSESpecification;
      if (enc) {
        expect(enc.SSEEnabled).toBe(true);
        expect(enc.KMSMasterKeyId).toBeUndefined();
      }
    }
  });
});

describe('QuotaTablesConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new QuotaTablesConstruct(stack, 'QT', { config });
    t = Template.fromStack(stack);
  });

  it('UserQuotas table has 5 GSIs', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-user-quotas',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'AssignmentTypeIndex' }),
        Match.objectLike({ IndexName: 'UserAssignmentIndex' }),
        Match.objectLike({ IndexName: 'RoleAssignmentIndex' }),
        Match.objectLike({ IndexName: 'UserOverrideIndex' }),
        Match.objectLike({ IndexName: 'AppRoleAssignmentIndex' }),
      ]),
    });
  });

  it('QuotaEvents table has TierEventIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-quota-events',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'TierEventIndex' }),
      ]),
    });
  });
});

describe('CostTrackingTablesConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new CostTrackingTablesConstruct(stack, 'CT', { config });
    t = Template.fromStack(stack);
  });

  it('SessionsMetadata has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-sessions-metadata',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('SessionsMetadata has 2 GSIs', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-sessions-metadata',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'UserTimestampIndex' }),
        Match.objectLike({ IndexName: 'SessionLookupIndex' }),
      ]),
    });
  });

  it('UserCostSummary has PeriodCostIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-user-cost-summary',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'PeriodCostIndex' }),
      ]),
    });
  });

  it('ManagedModels has ModelIdIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-managed-models',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'ModelIdIndex' }),
      ]),
    });
  });
});

describe('FileUploadConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new FileUploadConstruct(stack, 'FU', { config });
    t = Template.fromStack(stack);
  });

  it('bucket has lifecycle rules', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Id: 'transition-to-ia' }),
          Match.objectLike({ Id: 'transition-to-glacier' }),
          Match.objectLike({ Id: 'expire-objects' }),
          Match.objectLike({ Id: 'abort-incomplete-multipart' }),
        ]),
      },
    });
  });

  it('bucket blocks all public access', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it('table has SessionIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'SessionIndex' }),
      ]),
    });
  });

  it('table has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('table has DDB stream enabled', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      StreamSpecification: { StreamViewType: 'NEW_AND_OLD_IMAGES' },
    });
  });
});

describe('SharedConversationsConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new SharedConversationsConstruct(stack, 'SC', { config });
    t = Template.fromStack(stack);
  });

  it('has SessionShareIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'SessionShareIndex' }),
      ]),
    });
  });

  it('has OwnerShareIndex GSI with sort key', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'OwnerShareIndex',
          KeySchema: Match.arrayWith([
            Match.objectLike({ AttributeName: 'created_at', KeyType: 'RANGE' }),
          ]),
        }),
      ]),
    });
  });
});

describe('RagDataConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new RagDataConstruct(stack, 'Rag', { config });
    t = Template.fromStack(stack);
  });

  it('documents bucket is versioned', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });

  it('assistants table has 3 GSIs', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'OwnerStatusIndex' }),
        Match.objectLike({ IndexName: 'VisibilityStatusIndex' }),
        Match.objectLike({ IndexName: 'SharedWithIndex' }),
      ]),
    });
  });

  it('assistants table has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('vector index has correct dimension', () => {
    t.hasResourceProperties('AWS::S3Vectors::Index', {
      Dimension: 1024,
      DistanceMetric: 'cosine',
      DataType: 'float32',
    });
  });
});

describe('FineTuningDataConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new FineTuningDataConstruct(stack, 'FT', { config });
    t = Template.fromStack(stack);
  });

  it('jobs table has StatusIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-fine-tuning-jobs',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'StatusIndex' }),
      ]),
    });
  });

  it('data bucket has 30-day expiration', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Id: 'expire-objects', ExpirationInDays: 30 }),
        ]),
      },
    });
  });
});

describe('VoiceTicketConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new VoiceTicketConstruct(stack, 'Voice', { config });
    t = Template.fromStack(stack);
  });

  it('replay table has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('replay table uses jti as partition key', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [{ AttributeName: 'jti', KeyType: 'HASH' }],
    });
  });

  it('signing secret has 64-char password', () => {
    t.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: Match.objectLike({
        PasswordLength: 64,
        ExcludePunctuation: true,
      }),
    });
  });
});
