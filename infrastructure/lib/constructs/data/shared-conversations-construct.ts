import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

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
 *
 * The snapshot BODY (messages + metadata) is offloaded to the sibling S3
 * bucket, because a long conversation exceeds DynamoDB's 400 KB item limit
 * (PutItem ValidationException). The DynamoDB item keeps only the small
 * control fields plus an S3 pointer (`body_ref`); the bytes live in S3.
 * Mirrors the Memory Spaces / Artifacts / Skills S3-offload pattern.
 * Private, server-side-only (app-api reads/writes; never loaded cross-origin).
 * See docs/specs/share-large-conversations-s3-offload.md.
 */
export class SharedConversationsConstruct extends Construct {
  public readonly table: dynamodb.Table;
  public readonly bucket: s3.Bucket;

  constructor(
    scope: Construct,
    id: string,
    props: SharedConversationsConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    // Snapshot-body bucket — private, bytes-only, fetched server-side by
    // app-api. No expiration lifecycle rule: a share's object lives exactly
    // as long as its DynamoDB row and is deleted explicitly on revoke /
    // session cleanup. Age-based reaping would break live shares.
    this.bucket = new s3.Bucket(this, 'SharedConversationsBucket', {
      bucketName: getResourceName(config, 'shared-conversations'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: 'abort-stale-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

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

    new ssm.StringParameter(this, 'SharedConversationsBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/shares/shared-conversations-bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'Shared conversations snapshot-body S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
