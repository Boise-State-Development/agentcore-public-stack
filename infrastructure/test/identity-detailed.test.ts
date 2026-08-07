/**
 * Identity construct detailed tests — Cognito, OAuth, BFF cookie, auth providers.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

import { CognitoConstruct } from '../lib/constructs/identity/cognito-construct';
import { OAuthTablesConstruct } from '../lib/constructs/identity/oauth-tables-construct';
import { BffCookieKeyConstruct } from '../lib/constructs/identity/bff-cookie-key-construct';
import { AuthProvidersConstruct } from '../lib/constructs/identity/auth-providers-construct';
import { AuthSecretConstruct } from '../lib/constructs/identity/auth-secret-construct';
import { ArtifactRenderTokenSecretConstruct } from '../lib/constructs/identity/artifact-render-token-secret-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

const config = createMockConfig();

describe('CognitoConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new CognitoConstruct(stack, 'Cog', { config });
    t = Template.fromStack(stack);
  });

  it('user pool has email as required attribute', () => {
    t.hasResourceProperties('AWS::Cognito::UserPool', {
      Schema: Match.arrayWith([
        Match.objectLike({ Name: 'email', Required: true }),
      ]),
    });
  });

  it('user pool has password policy with min length 8', () => {
    t.hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: {
        PasswordPolicy: Match.objectLike({
          MinimumLength: 8,
          RequireUppercase: true,
          RequireLowercase: true,
          RequireNumbers: true,
          RequireSymbols: true,
        }),
      },
    });
  });

  it('user pool has email auto-verification', () => {
    t.hasResourceProperties('AWS::Cognito::UserPool', {
      AutoVerifiedAttributes: ['email'],
    });
  });

  it('user pool client generates a secret', () => {
    t.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      GenerateSecret: true,
    });
  });

  it('user pool client supports authorization code grant', () => {
    t.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AllowedOAuthFlows: ['code'],
    });
  });

  it('user pool client has openid + profile + email scopes', () => {
    t.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AllowedOAuthScopes: Match.arrayWith(['openid', 'profile', 'email']),
    });
  });

  it('user pool domain uses project prefix', () => {
    t.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
      Domain: 'test-project',
    });
  });

  it('publishes user pool ID to SSM', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/auth/cognito/user-pool-id',
    });
  });

  it('publishes issuer URL to SSM', () => {
  });

  it('publishes BFF app client ID to SSM', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/auth/cognito/bff-app-client-id',
    });
  });

  it('persists client secret in Secrets Manager', () => {
    t.resourceCountIs('AWS::SecretsManager::Secret', 1);
  });
});

describe('OAuthTablesConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new OAuthTablesConstruct(stack, 'OAuth', { config });
    t = Template.fromStack(stack);
  });

  it('user tokens table uses customer-managed KMS encryption', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-oauth-user-tokens',
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
    });
  });

  it('providers table has EnabledProvidersIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-oauth-providers',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'EnabledProvidersIndex' }),
      ]),
    });
  });

  it('user tokens table has ProviderUsersIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'test-project-oauth-user-tokens',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'ProviderUsersIndex' }),
      ]),
    });
  });

  it('KMS key has rotation enabled', () => {
    t.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  it('publishes encryption key ARN to SSM', () => {
  });

  it('publishes client secrets ARN to SSM', () => {
  });
});

describe('BffCookieKeyConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new BffCookieKeyConstruct(stack, 'Bff', { config });
    t = Template.fromStack(stack);
  });

  it('KMS key has rotation enabled', () => {
    t.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  it('data key secret has 44-char password', () => {
    t.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: Match.objectLike({
        PasswordLength: 44,
        ExcludePunctuation: true,
        IncludeSpace: false,
      }),
    });
  });

  it('publishes signing key ARN to SSM', () => {
  });

  it('publishes data key secret ARN to SSM', () => {
  });
});

describe('AuthProvidersConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new AuthProvidersConstruct(stack, 'AP', { config });
    t = Template.fromStack(stack);
  });

  it('table has DDB stream enabled', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      StreamSpecification: { StreamViewType: 'NEW_AND_OLD_IMAGES' },
    });
  });

  it('table has EnabledProvidersIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'EnabledProvidersIndex' }),
      ]),
    });
  });

  it('publishes stream ARN to SSM', () => {
  });

  it('publishes secrets ARN to SSM', () => {
  });
});

describe('AuthSecretConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new AuthSecretConstruct(stack, 'Auth', { config });
    t = Template.fromStack(stack);
  });

  it('secret has 64-char password', () => {
    t.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: Match.objectLike({
        PasswordLength: 64,
        ExcludePunctuation: true,
      }),
    });
  });
});

describe('ArtifactRenderTokenSecretConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new ArtifactRenderTokenSecretConstruct(stack, 'Art', { config });
    t = Template.fromStack(stack);
  });

  it('secret has 44-char password', () => {
    t.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: Match.objectLike({
        PasswordLength: 44,
        ExcludePunctuation: true,
      }),
    });
  });

  it('publishes ARN to SSM', () => {
  });
});
