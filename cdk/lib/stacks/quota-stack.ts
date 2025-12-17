/**
 * CDK Stack for Quota Management DynamoDB Tables
 *
 * Creates:
 * - UserQuotas table with 3 GSIs (AssignmentTypeIndex, UserAssignmentIndex, RoleAssignmentIndex)
 * - QuotaEvents table with 1 GSI (TierEventIndex)
 *
 * All tables use PAY_PER_REQUEST billing for cost optimization.
 */

import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

export interface QuotaStackProps extends cdk.StackProps {
  environment: string;
}

export class QuotaStack extends cdk.Stack {
  public readonly userQuotasTable: dynamodb.Table;
  public readonly quotaEventsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: QuotaStackProps) {
    super(scope, id, props);

    const { environment } = props;

    // ========== UserQuotas Table ==========

    this.userQuotasTable = new dynamodb.Table(this, 'UserQuotasTable', {
      tableName: `UserQuotas-${environment}`,
      partitionKey: {
        name: 'PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'SK',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    // GSI1: AssignmentTypeIndex
    // Query assignments by type, sorted by priority
    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'AssignmentTypeIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI2: UserAssignmentIndex
    // Query direct user assignments (O(1) lookup)
    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserAssignmentIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI3: RoleAssignmentIndex
    // Query role-based assignments, sorted by priority
    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'RoleAssignmentIndex',
      partitionKey: {
        name: 'GSI3PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ========== QuotaEvents Table ==========

    this.quotaEventsTable = new dynamodb.Table(this, 'QuotaEventsTable', {
      tableName: `QuotaEvents-${environment}`,
      partitionKey: {
        name: 'PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'SK',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    // GSI5: TierEventIndex
    // Query events by tier for analytics (Phase 2)
    this.quotaEventsTable.addGlobalSecondaryIndex({
      indexName: 'TierEventIndex',
      partitionKey: {
        name: 'GSI5PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI5SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ========== Outputs ==========

    new cdk.CfnOutput(this, 'UserQuotasTableName', {
      value: this.userQuotasTable.tableName,
      description: 'UserQuotas table name',
      exportName: `UserQuotasTable-${environment}`,
    });

    new cdk.CfnOutput(this, 'QuotaEventsTableName', {
      value: this.quotaEventsTable.tableName,
      description: 'QuotaEvents table name',
      exportName: `QuotaEventsTable-${environment}`,
    });

    // ========== Tags ==========

    cdk.Tags.of(this).add('Environment', environment);
    cdk.Tags.of(this).add('Service', 'quota-management');
    cdk.Tags.of(this).add('Phase', '1');
    cdk.Tags.of(this).add('ManagedBy', 'CDK');
  }
}
