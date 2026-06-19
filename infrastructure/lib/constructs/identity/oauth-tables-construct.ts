import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface OAuthTablesConstructProps {
  config: AppConfig;
}

/**
 * OAuthTablesConstruct — OAuth provider tables, KMS key, client secrets.
 *
 *   - OAuthProvidersTable          — admin-configured OAuth provider settings
 *   - OAuthUserTokensTable         — user-connected OAuth tokens (KMS encrypted)
 *   - OAuthTokenEncryptionKey      — CMK encrypting the user-tokens table
 *   - OAuthClientSecretsSecret     — Secrets Manager bag of provider client secrets
 */
export class OAuthTablesConstruct extends Construct {
  public readonly providersTable: dynamodb.Table;
  public readonly userTokensTable: dynamodb.Table;
  public readonly tokenEncryptionKey: kms.Key;
  public readonly clientSecretsSecret: secretsmanager.Secret;

  constructor(
    scope: Construct,
    id: string,
    props: OAuthTablesConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    // KMS Key for encrypting OAuth user tokens at rest
    this.tokenEncryptionKey = new kms.Key(this, 'OAuthTokenEncryptionKey', {
      alias: getResourceName(config, 'oauth-token-key'),
      description: 'KMS key for encrypting OAuth user tokens at rest',
      enableKeyRotation: true,
      removalPolicy: getRemovalPolicy(config),
    });

    // OAuth Providers Table
    this.providersTable = new dynamodb.Table(this, 'OAuthProvidersTable', {
      tableName: getResourceName(config, 'oauth-providers'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.providersTable.addGlobalSecondaryIndex({
      indexName: 'EnabledProvidersIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // OAuth User Tokens Table - KMS-encrypted
    this.userTokensTable = new dynamodb.Table(this, 'OAuthUserTokensTable', {
      tableName: getResourceName(config, 'oauth-user-tokens'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.tokenEncryptionKey,
    });

    this.userTokensTable.addGlobalSecondaryIndex({
      indexName: 'ProviderUsersIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // OAuth client secrets bag (JSON: {provider_id: secret})
    this.clientSecretsSecret = new secretsmanager.Secret(
      this,
      'OAuthClientSecretsSecret',
      {
        secretName: getResourceName(config, 'oauth-client-secrets'),
        description:
          'OAuth provider client secrets (JSON: {provider_id: secret})',
        removalPolicy: getRemovalPolicy(config),
      },
    );

    // ── SSM publications (consumed by restore tooling, app-api/inference-api runtime) ──
    new ssm.StringParameter(this, 'OAuthProvidersTableNameParameter', {
      parameterName: `/${config.projectPrefix}/oauth/providers-table-name`,
      stringValue: this.providersTable.tableName,
      description: 'OAuth providers table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'OAuthUserTokensTableNameParameter', {
      parameterName: `/${config.projectPrefix}/oauth/user-tokens-table-name`,
      stringValue: this.userTokensTable.tableName,
      description: 'OAuth user tokens table name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
