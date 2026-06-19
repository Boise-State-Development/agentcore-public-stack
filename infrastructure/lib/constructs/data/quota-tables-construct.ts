import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface QuotaTablesConstructProps {
  config: AppConfig;
}

/**
 * QuotaTablesConstruct — DynamoDB tables backing quota management.
 *
 *   - UserQuotasTable   — quota assignments for users and roles
 *                         (5 GSIs covering assignment-by-type,
 *                          direct-user, role-based, override, app-role)
 *   - QuotaEventsTable  — quota usage event tracking with tier index
 */
export class QuotaTablesConstruct extends Construct {
  public readonly userQuotasTable: dynamodb.Table;
  public readonly quotaEventsTable: dynamodb.Table;

  constructor(
    scope: Construct,
    id: string,
    props: QuotaTablesConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.userQuotasTable = new dynamodb.Table(this, 'UserQuotasTable', {
      tableName: getResourceName(config, 'user-quotas'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'AssignmentTypeIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserAssignmentIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'RoleAssignmentIndex',
      partitionKey: { name: 'GSI3PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI3SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserOverrideIndex',
      partitionKey: { name: 'GSI4PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI4SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'AppRoleAssignmentIndex',
      partitionKey: { name: 'GSI6PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI6SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new ssm.StringParameter(this, 'UserQuotasTableNameParameter', {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-name`,
      stringValue: this.userQuotasTable.tableName,
      description: 'UserQuotas table name',
      tier: ssm.ParameterTier.STANDARD,
    });


    this.quotaEventsTable = new dynamodb.Table(this, 'QuotaEventsTable', {
      tableName: getResourceName(config, 'quota-events'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.quotaEventsTable.addGlobalSecondaryIndex({
      indexName: 'TierEventIndex',
      partitionKey: { name: 'GSI5PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI5SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new ssm.StringParameter(this, 'QuotaEventsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-name`,
      stringValue: this.quotaEventsTable.tableName,
      description: 'Quota events table name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
