import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface AuthProvidersConstructProps {
  config: AppConfig;
}

/**
 * AuthProvidersConstruct — OIDC authentication provider configuration.
 *
 *   - AuthProvidersTable           — provider config rows + DDB stream
 *                                    (NEW_AND_OLD_IMAGES) for change watch
 *   - EnabledProvidersIndex (GSI1) — query enabled providers for login page
 *   - AuthProviderSecretsSecret    — Secrets Manager bag of provider client
 *                                    secrets (JSON: {provider_id: secret})
 *
 * The DDB stream ARN is published to SSM so consumers can subscribe to
 * provider config changes for hot-reload.
 */
export class AuthProvidersConstruct extends Construct {
  public readonly providersTable: dynamodb.Table;
  public readonly secretsSecret: secretsmanager.Secret;

  constructor(
    scope: Construct,
    id: string,
    props: AuthProvidersConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.providersTable = new dynamodb.Table(this, 'AuthProvidersTable', {
      tableName: getResourceName(config, 'auth-providers'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.providersTable.addGlobalSecondaryIndex({
      indexName: 'EnabledProvidersIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });




    this.secretsSecret = new secretsmanager.Secret(
      this,
      'AuthProviderSecretsSecret',
      {
        secretName: getResourceName(config, 'auth-provider-secrets'),
        description:
          'OIDC authentication provider client secrets ' +
          '(JSON: {provider_id: secret})',
        removalPolicy: getRemovalPolicy(config),
      },
    );

    new ssm.StringParameter(this, 'AuthProvidersTableNameParameter', {
      parameterName: `/${config.projectPrefix}/auth/auth-providers-table-name`,
      stringValue: this.providersTable.tableName,
      description: 'Auth providers table name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
