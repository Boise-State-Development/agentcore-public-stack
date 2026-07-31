import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface AuditLogConstructProps {
  config: AppConfig;
}

/**
 * AuditLogConstruct — the durable trail of administrative mutations.
 *
 * Backs `apis/shared/audit`. Three access patterns, one per index:
 *
 *   - base table   `PK = AUDIT#<targetType>#<targetId>` — history of one role
 *   - ActorIndex   `GSI1PK = ACTOR#<userId>`            — what one admin did
 *   - RecentIndex  `GSI2PK = AUDIT#<YYYY-MM>`           — recent activity feed
 *
 * The recent feed is **month-sharded** rather than keyed on a single constant.
 * One `AUDIT#ALL` partition would absorb every administrative write on the
 * platform for the life of the deployment, which is the textbook hot-partition
 * shape; a month also happens to be a sensible "load older" step for the
 * console.
 *
 * `expiresAt` gives records a one-year TTL (`RETENTION_DAYS` in
 * `apis/shared/audit/models.py` — the two must agree). Long enough to answer
 * "who granted this last fall?", bounded so the table does not grow forever.
 *
 * Point-in-time recovery is on: this is the record of who changed permissions,
 * so losing it to an operational mistake is worse than the cost of keeping it.
 */
export class AuditLogConstruct extends Construct {
  public readonly auditLogTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: AuditLogConstructProps) {
    super(scope, id);

    const { config } = props;

    this.auditLogTable = new dynamodb.Table(this, 'AuditLogTable', {
      tableName: getResourceName(config, 'audit-log'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expiresAt',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.auditLogTable.addGlobalSecondaryIndex({
      indexName: 'ActorIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.auditLogTable.addGlobalSecondaryIndex({
      indexName: 'RecentIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new ssm.StringParameter(this, 'AuditLogTableNameParameter', {
      parameterName: `/${config.projectPrefix}/audit/audit-log-table-name`,
      stringValue: this.auditLogTable.tableName,
      description: 'Administrative audit log table name',
    });
  }
}
