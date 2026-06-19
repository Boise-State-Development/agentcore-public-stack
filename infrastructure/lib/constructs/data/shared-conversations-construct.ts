import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface SharedConversationsConstructProps {
  config: AppConfig;
}

/**
 * SharedConversationsConstruct — point-in-time snapshots of shared
 * conversations (the "share" feature).
 *
 * Each share is identified by a unique share_id and contains the
 * conversation metadata and messages at the time of sharing.
 *
 *   PK: share_id
 *   GSI: SessionShareIndex     — lookup by original session_id
 *   GSI: OwnerShareIndex       — list shares by owner, sorted by created_at
 */
export class SharedConversationsConstruct extends Construct {
  public readonly table: dynamodb.Table;

  constructor(
    scope: Construct,
    id: string,
    props: SharedConversationsConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.table = new dynamodb.Table(this, 'SharedConversationsTable', {
      tableName: getResourceName(config, 'shared-conversations'),
      partitionKey: {
        name: 'share_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'SessionShareIndex',
      partitionKey: {
        name: 'session_id',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'OwnerShareIndex',
      partitionKey: {
        name: 'owner_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new ssm.StringParameter(this, 'SharedConversationsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/shares/shared-conversations-table-name`,
      stringValue: this.table.tableName,
      description: 'Shared conversations table name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
