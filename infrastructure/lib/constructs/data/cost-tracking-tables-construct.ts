import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface CostTrackingTablesConstructProps {
  config: AppConfig;
}

/**
 * CostTrackingTablesConstruct — DynamoDB tables backing cost tracking
 * and the admin cost dashboard.
 *
 *   - SessionsMetadataTable  — message-level metadata for cost tracking
 *                              (UserTimestampIndex + SessionLookupIndex)
 *   - UserCostSummaryTable   — pre-aggregated user-level cost summaries
 *                              (PeriodCostIndex enables top-N queries)
 *   - SystemCostRollupTable  — pre-aggregated system-wide metrics
 *   - ManagedModelsTable     — model registry + per-model pricing
 *                              (ModelIdIndex enables duplicate checking)
 */
export class CostTrackingTablesConstruct extends Construct {
  public readonly sessionsMetadataTable: dynamodb.Table;
  public readonly userCostSummaryTable: dynamodb.Table;
  public readonly systemCostRollupTable: dynamodb.Table;
  public readonly managedModelsTable: dynamodb.Table;

  constructor(
    scope: Construct,
    id: string,
    props: CostTrackingTablesConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    // SessionsMetadata Table - message-level metadata
    this.sessionsMetadataTable = new dynamodb.Table(
      this,
      'SessionsMetadataTable',
      {
        tableName: getResourceName(config, 'sessions-metadata'),
        partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        timeToLiveAttribute: 'ttl',
        removalPolicy: getRemovalPolicy(config),
        encryption: dynamodb.TableEncryption.AWS_MANAGED,
      },
    );

    this.sessionsMetadataTable.addGlobalSecondaryIndex({
      indexName: 'UserTimestampIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.sessionsMetadataTable.addGlobalSecondaryIndex({
      indexName: 'SessionLookupIndex',
      partitionKey: { name: 'GSI_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI_SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });



    // UserCostSummary Table
    this.userCostSummaryTable = new dynamodb.Table(
      this,
      'UserCostSummaryTable',
      {
        tableName: getResourceName(config, 'user-cost-summary'),
        partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        removalPolicy: getRemovalPolicy(config),
        encryption: dynamodb.TableEncryption.AWS_MANAGED,
      },
    );

    this.userCostSummaryTable.addGlobalSecondaryIndex({
      indexName: 'PeriodCostIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: [
        'userId',
        'totalCost',
        'totalRequests',
        'lastUpdated',
      ],
    });



    // SystemCostRollup Table
    this.systemCostRollupTable = new dynamodb.Table(
      this,
      'SystemCostRollupTable',
      {
        tableName: getResourceName(config, 'system-cost-rollup'),
        partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        removalPolicy: getRemovalPolicy(config),
        encryption: dynamodb.TableEncryption.AWS_MANAGED,
      },
    );



    // ManagedModels Table
    this.managedModelsTable = new dynamodb.Table(this, 'ManagedModelsTable', {
      tableName: getResourceName(config, 'managed-models'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.managedModelsTable.addGlobalSecondaryIndex({
      indexName: 'ModelIdIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new ssm.StringParameter(this, 'ManagedModelsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/admin/managed-models-table-name`,
      stringValue: this.managedModelsTable.tableName,
      description: 'ManagedModels table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Cost-tracking tables (consumed by restore tooling, app-api/inference-api runtime)
    new ssm.StringParameter(this, 'SessionsMetadataTableNameParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-name`,
      stringValue: this.sessionsMetadataTable.tableName,
      description: 'Sessions metadata table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserCostSummaryTableNameParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-name`,
      stringValue: this.userCostSummaryTable.tableName,
      description: 'User cost summary table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SystemCostRollupTableNameParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-name`,
      stringValue: this.systemCostRollupTable.tableName,
      description: 'System cost rollup table name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
