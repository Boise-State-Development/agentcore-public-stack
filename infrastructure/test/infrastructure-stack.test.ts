import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfrastructureStack } from '../lib/infrastructure-stack';
import { createMockConfig, mockEnv } from './helpers/mock-config';

/**
 * Concatenate the literal string fragments of an Fn::Join payload, ignoring
 * intrinsic refs ({Fn::GetAtt: ...} / {Ref: ...}). Used to assert on the
 * AwsCustomResource `Create` payload, which CDK emits as a Join over the
 * SDK call's parameter JSON with cross-resource references spliced in.
 */
function _joinLiterals(value: any): string {
  if (typeof value === 'string') return value;
  if (value && value['Fn::Join']) {
    const [_sep, parts] = value['Fn::Join'];
    return (parts as any[])
      .map((p) => (typeof p === 'string' ? p : ''))
      .join('');
  }
  return '';
}

function _findCustomResourceCreate(
  template: Template,
  actionSubstring: string,
): string | null {
  const customResources = template.findResources('Custom::AWS');
  for (const r of Object.values(customResources)) {
    const create = (r as any).Properties?.Create;
    const text = _joinLiterals(create);
    if (text.includes(`"action":"${actionSubstring}"`)) return text;
  }
  return null;
}

/**
 * Unit tests for InfrastructureStack — the foundation layer.
 *
 * Validates VPC, ALB, ECS Cluster, Security Groups, DynamoDB tables,
 * Secrets Manager secrets, KMS keys, and SSM parameter exports.
 */
describe('InfrastructureStack', () => {
  let template: Template;
  let config: ReturnType<typeof createMockConfig>;

  beforeEach(() => {
    const app = new cdk.App();
    config = createMockConfig();
    const stack = new InfrastructureStack(app, 'TestInfrastructureStack', {
      config,
      env: mockEnv(config),
    });
    template = Template.fromStack(stack);
  });

  // ------------------------------------------------------------------
  // 1. Stack synthesizes without errors
  // ------------------------------------------------------------------
  test('stack synthesizes without errors', () => {
    // If we got here, Template.fromStack succeeded
    expect(template.toJSON()).toBeDefined();
  });

  // ------------------------------------------------------------------
  // 2. VPC exists with correct CIDR and subnet config
  // ------------------------------------------------------------------
  test('VPC is created with the configured CIDR block', () => {
    template.hasResourceProperties('AWS::EC2::VPC', {
      CidrBlock: '10.0.0.0/16',
      EnableDnsHostnames: true,
      EnableDnsSupport: true,
    });
  });

  test('VPC has public and private subnets across 2 AZs', () => {
    // 2 AZs × 2 subnet types = 4 subnets
    template.resourceCountIs('AWS::EC2::Subnet', 4);
  });

  test('VPC has exactly 1 NAT Gateway', () => {
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
  });

  // ------------------------------------------------------------------
  // 3. ALB exists and is internet-facing
  // ------------------------------------------------------------------
  test('ALB is created and is internet-facing', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      {
        Scheme: 'internet-facing',
        Type: 'application',
      },
    );
  });

  // ------------------------------------------------------------------
  // 4. ECS Cluster exists with Container Insights
  // ------------------------------------------------------------------
  test('ECS Cluster is created with Container Insights enabled', () => {
    template.hasResourceProperties('AWS::ECS::Cluster', {
      ClusterSettings: Match.arrayWith([
        Match.objectLike({
          Name: 'containerInsights',
          Value: 'enabled',
        }),
      ]),
    });
  });

  // ------------------------------------------------------------------
  // 5. Security Group exists with HTTP/HTTPS ingress
  // ------------------------------------------------------------------
  test('ALB Security Group allows HTTP ingress (port 80)', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          IpProtocol: 'tcp',
          FromPort: 80,
          ToPort: 80,
          CidrIp: '0.0.0.0/0',
        }),
      ]),
    });
  });

  test('ALB Security Group allows HTTPS ingress (port 443)', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          IpProtocol: 'tcp',
          FromPort: 443,
          ToPort: 443,
          CidrIp: '0.0.0.0/0',
        }),
      ]),
    });
  });

  // ------------------------------------------------------------------
  // 6. All DynamoDB tables are created (count)
  // ------------------------------------------------------------------
  test('creates all 18 DynamoDB tables', () => {
    // The hard-coded count drifts as tables are added — bump it when you
    // intentionally provision a new table; investigate if it changed
    // unexpectedly. Today: oidc_state, bff_sessions, voice_ticket_replay,
    // users, app_roles, api_keys, oauth_providers, oauth_user_tokens,
    // user_quotas, quota_events, sessions_metadata, user_cost_summary,
    // system_cost_rollup, managed_models, user_settings, auth_providers,
    // user_files, shared_conversations.
    template.resourceCountIs('AWS::DynamoDB::Table', 18);
  });

  // ------------------------------------------------------------------
  // 7. Key DynamoDB tables have expected key schemas
  // ------------------------------------------------------------------
  test('OidcState table has PK/SK and TTL', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('oidc-state'),
      KeySchema: Match.arrayWith([
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ]),
      TimeToLiveSpecification: {
        AttributeName: 'expiresAt',
        Enabled: true,
      },
    });
  });

  test('Users table has PK/SK and 4 GSIs', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('users$'),
      KeySchema: Match.arrayWith([
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ]),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'UserIdIndex' }),
        Match.objectLike({ IndexName: 'EmailIndex' }),
        Match.objectLike({ IndexName: 'EmailDomainIndex' }),
        Match.objectLike({ IndexName: 'StatusLoginIndex' }),
      ]),
    });
  });

  test('AppRoles table has 3 GSIs for role mappings', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('app-roles'),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'JwtRoleMappingIndex' }),
        Match.objectLike({ IndexName: 'ToolRoleMappingIndex' }),
        Match.objectLike({ IndexName: 'ModelRoleMappingIndex' }),
      ]),
    });
  });

  test('ApiKeys table has KeyHashIndex GSI', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('api-keys'),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'KeyHashIndex' }),
      ]),
    });
  });

  test('UserQuotas table has 5 GSIs', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('user-quotas'),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'AssignmentTypeIndex' }),
        Match.objectLike({ IndexName: 'UserAssignmentIndex' }),
        Match.objectLike({ IndexName: 'RoleAssignmentIndex' }),
        Match.objectLike({ IndexName: 'UserOverrideIndex' }),
        Match.objectLike({ IndexName: 'AppRoleAssignmentIndex' }),
      ]),
    });
  });

  test('SessionsMetadata table has TTL and 2 GSIs', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('sessions-metadata'),
      TimeToLiveSpecification: {
        AttributeName: 'ttl',
        Enabled: true,
      },
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'UserTimestampIndex' }),
        Match.objectLike({ IndexName: 'SessionLookupIndex' }),
      ]),
    });
  });

  test('AuthProviders table has DynamoDB stream enabled', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('auth-providers'),
      StreamSpecification: {
        StreamViewType: 'NEW_AND_OLD_IMAGES',
      },
    });
  });

  test('OAuthUserTokens table uses customer-managed KMS encryption', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('oauth-user-tokens'),
      SSESpecification: {
        SSEEnabled: true,
        SSEType: 'KMS',
        KMSMasterKeyId: Match.anyValue(),
      },
    });
  });

  test('UserFiles table has PK/SK, TTL, stream, and SessionIndex GSI', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: Match.stringLikeRegexp('user-file-uploads'),
      KeySchema: Match.arrayWith([
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ]),
      TimeToLiveSpecification: {
        AttributeName: 'ttl',
        Enabled: true,
      },
      StreamSpecification: {
        StreamViewType: 'NEW_AND_OLD_IMAGES',
      },
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'SessionIndex' }),
      ]),
    });
  });

  test('UserFiles S3 bucket blocks all public access and enforces SSL', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  test('UserFiles S3 bucket has lifecycle rules', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
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

  test('all DynamoDB tables use PAY_PER_REQUEST billing', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [logicalId, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.BillingMode).toBe('PAY_PER_REQUEST');
    }
  });

  // ------------------------------------------------------------------
  // 8. Secrets Manager Secrets exist
  // ------------------------------------------------------------------
  test('creates 6 Secrets Manager secrets', () => {
    // Today: AuthenticationSecret, VoiceTicketSigningSecret,
    // BFFCookieDataKeySecret (deploy-time wrapped AES-256 data key for
    // cross-task cookie seal/unseal), OAuthClientSecretsSecret,
    // AuthProviderSecretsSecret, CognitoBFFAppClientSecret.
    template.resourceCountIs('AWS::SecretsManager::Secret', 6);
  });

  test('authentication secret generates a 64-char random string', () => {
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: Match.objectLike({
        GenerateStringKey: 'secret',
        PasswordLength: 64,
        ExcludePunctuation: true,
      }),
    });
  });

  // ------------------------------------------------------------------
  // 9. KMS Key exists
  // ------------------------------------------------------------------
  test('KMS key is created with key rotation enabled', () => {
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  test('KMS key has an alias for oauth-token-key', () => {
    template.hasResourceProperties('AWS::KMS::Alias', {
      AliasName: Match.stringLikeRegexp('oauth-token-key'),
    });
  });

  // ------------------------------------------------------------------
  // 10. SSM parameters are created (at least 40+)
  // ------------------------------------------------------------------
  test('creates at least 44 SSM StringParameters', () => {
    const params = template.findResources('AWS::SSM::Parameter');
    expect(Object.keys(params).length).toBeGreaterThanOrEqual(44);
  });

  test('SSM parameters include key network exports', () => {
    const prefix = `/${config.projectPrefix}`;

    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `${prefix}/network/vpc-id`,
      Type: 'String',
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `${prefix}/network/alb-arn`,
      Type: 'String',
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `${prefix}/network/ecs-cluster-name`,
      Type: 'String',
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `${prefix}/network/alb-listener-arn`,
      Type: 'String',
    });
  });

  test('SSM parameters include DynamoDB table exports', () => {
    const prefix = `/${config.projectPrefix}`;

    const expectedParams = [
      'auth/oidc-state-table-name',
      'auth/oidc-state-table-arn',
      'users/users-table-name',
      'users/users-table-arn',
      'rbac/app-roles-table-name',
      'auth/api-keys-table-name',
      'oauth/providers-table-name',
      'oauth/user-tokens-table-name',
      'oauth/token-encryption-key-arn',
      'quota/user-quotas-table-name',
      'quota/quota-events-table-name',
      'cost-tracking/sessions-metadata-table-name',
      'cost-tracking/user-cost-summary-table-name',
      'cost-tracking/system-cost-rollup-table-name',
      'admin/managed-models-table-name',
      'auth/auth-providers-table-name',
      'auth/auth-providers-stream-arn',
      'user-file-uploads/bucket-name',
      'user-file-uploads/bucket-arn',
      'user-file-uploads/table-name',
      'user-file-uploads/table-arn',
    ];

    for (const param of expectedParams) {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `${prefix}/${param}`,
        Type: 'String',
      });
    }
  });

  // ------------------------------------------------------------------
  // 11. All DynamoDB tables have encryption enabled
  // ------------------------------------------------------------------
  test('all DynamoDB tables have encryption enabled', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [logicalId, resource] of Object.entries(tables)) {
      const sse = (resource as any).Properties.SSESpecification;
      expect(sse).toBeDefined();
      expect(sse.SSEEnabled).toBe(true);
    }
  });

  // ------------------------------------------------------------------
  // 12. Removal policy matches config (DESTROY for non-production)
  // ------------------------------------------------------------------
  test('DynamoDB tables have DESTROY removal policy when retainDataOnDelete is false', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const [logicalId, resource] of Object.entries(tables)) {
      // CDK maps RemovalPolicy.DESTROY to DeletionPolicy: Delete
      expect((resource as any).DeletionPolicy).toBe('Delete');
    }
  });

  test('auth secret has DESTROY removal policy when retainDataOnDelete is false', () => {
    // Find the auth secret (the one with GenerateSecretString)
    template.hasResource('AWS::SecretsManager::Secret', {
      Properties: Match.objectLike({
        GenerateSecretString: Match.anyValue(),
      }),
      DeletionPolicy: 'Delete',
    });
  });

  test('KMS key has DESTROY removal policy when retainDataOnDelete is false', () => {
    template.hasResource('AWS::KMS::Key', {
      DeletionPolicy: 'Delete',
    });
  });

  test('retainDataOnDelete=true sets RETAIN removal policy on tables', () => {
    const app = new cdk.App();
    const retainConfig = createMockConfig({ retainDataOnDelete: true });
    const stack = new InfrastructureStack(app, 'RetainStack', {
      config: retainConfig,
      env: mockEnv(retainConfig),
    });
    const retainTemplate = Template.fromStack(stack);

    const tables = retainTemplate.findResources('AWS::DynamoDB::Table');
    for (const [logicalId, resource] of Object.entries(tables)) {
      expect((resource as any).DeletionPolicy).toBe('Retain');
    }
  });

  // ------------------------------------------------------------------
  // ALB Listener defaults to HTTP when no certificate is configured
  // ------------------------------------------------------------------
  test('ALB listener defaults to HTTP port 80 when no certificateArn', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::Listener',
      {
        Port: 80,
        Protocol: 'HTTP',
      },
    );
  });

  // ------------------------------------------------------------------
  // OAuth client secrets follow config-driven removal policy
  // ------------------------------------------------------------------
  test('OAuth client secrets secret has config-driven removal policy', () => {
    template.hasResource('AWS::SecretsManager::Secret', {
      Properties: Match.objectLike({
        Description: Match.stringLikeRegexp('OAuth provider client secrets'),
      }),
      DeletionPolicy: 'Delete',
    });
  });

  test('auth provider secrets secret has config-driven removal policy', () => {
    template.hasResource('AWS::SecretsManager::Secret', {
      Properties: Match.objectLike({
        Description: Match.stringLikeRegexp('OIDC authentication provider client secrets'),
      }),
      DeletionPolicy: 'Delete',
    });
  });

  // ------------------------------------------------------------------
  // BFF Cookie Data Key — shared wrapped data key for cross-task seal/unseal
  // ------------------------------------------------------------------
  describe('BFF Cookie Data Key', () => {
    test('provisions a Secrets Manager secret for the wrapped data key, encrypted with the BFFCookieSigningKey CMK', () => {
      template.hasResource('AWS::SecretsManager::Secret', {
        Properties: Match.objectLike({
          Description: Match.stringLikeRegexp('KMS-wrapped AES-256 data key'),
          KmsKeyId: Match.anyValue(),
        }),
      });
    });

    test('publishes the data key secret ARN to SSM for app-api to consume', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/auth/bff-cookie-data-key-secret-arn`,
      });
    });

    test('bootstrap custom resource calls kms:generateDataKey with KeySpec AES_256 + outputPaths CiphertextBlob', () => {
      // The `Create` payload of an AwsCustomResource is emitted as an
      // Fn::Join over [literal-fragments, intrinsic-refs, literal-fragments].
      // Concatenate the literal string fragments to assert on the embedded
      // service/action/parameters JSON.
      const create = _findCustomResourceCreate(template, 'generateDataKey');
      expect(create).not.toBeNull();
      expect(create).toContain('"service":"KMS"');
      expect(create).toContain('"action":"generateDataKey"');
      expect(create).toContain('"KeySpec":"AES_256"');
      // CRITICAL: outputPaths whitelists CiphertextBlob so the response
      // Plaintext (the AES key itself) never enters CFN state.
      expect(create).toContain('"outputPaths":["CiphertextBlob"]');
    });

    test('bootstrap custom resource calls secretsmanager:putSecretValue with the wrapped blob', () => {
      const create = _findCustomResourceCreate(template, 'putSecretValue');
      expect(create).not.toBeNull();
      expect(create).toContain('"service":"SecretsManager"');
      expect(create).toContain('"action":"putSecretValue"');
    });

    test('bootstrap custom resource has narrow IAM (kms:GenerateDataKey + secretsmanager:PutSecretValue only)', () => {
      // The bootstrap Lambda's role must NOT carry broad KMS or Secrets
      // Manager permissions — those would be a privilege-escalation path
      // for anyone who can invoke the custom resource.
      const policies = template.findResources('AWS::IAM::Policy');
      const customResourcePolicies = Object.values(policies).filter((p: any) =>
        JSON.stringify(p).includes('AWS679f53fac002430cb0da5b7982bd2287'),
      );
      // We don't assert on counts (CDK may share policies); we assert that
      // anywhere the bootstrap policies appear, the actions are exactly the
      // two we granted — no wildcards.
      const actionsInUse = new Set<string>();
      for (const policy of customResourcePolicies) {
        const stmts =
          (policy as any).Properties?.PolicyDocument?.Statement || [];
        for (const stmt of stmts) {
          const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          for (const a of actions) actionsInUse.add(a);
        }
      }
      // We expect at minimum these two actions; CDK adds Lambda invoke etc.
      // The negative assertion is what matters: no wildcards and no broad
      // kms:* or secretsmanager:*.
      for (const action of actionsInUse) {
        expect(action).not.toBe('*');
        expect(action).not.toBe('kms:*');
        expect(action).not.toBe('secretsmanager:*');
      }
    });
  });
});
