import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface MemorySpacesConstructProps {
  config: AppConfig;
}

/**
 * MemorySpacesConstruct — storage backing the "Memory Spaces" feature.
 *
 * Two resources, one construct:
 *   - S3 content bucket for memory-space files (mirrors the skill
 *     reference-file bucket: private, bytes-only, fetched server-side).
 *   - DynamoDB single-table (PK/SK) with two GSIs — OwnerIndex (GSI1) and
 *     MemberIndex (GSI2) — for owner/member reverse lookups.
 *
 * Private — the bucket is only ever read/written server-side by app-api
 * and the inference-api runtime. No CORS; never loaded cross-origin.
 *
 * Lifecycle:
 *   - Failed multipart uploads aborted after 7 days.
 *   (No expiration rule: memory-space files persist for the life of the
 *   space and are removed explicitly by the delete path.)
 *
 * The bucket/table are threaded to the compute roles via
 * `PlatformComputeRefs.memorySpacesBucket` / `.memorySpacesTable` (typed
 * refs, not SSM) — see the CLAUDE.md File Creation Rules.
 */
export class MemorySpacesConstruct extends Construct {
  public readonly bucket: s3.Bucket;
  public readonly table: dynamodb.Table;

  constructor(
    scope: Construct,
    id: string,
    props: MemorySpacesConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.bucket = new s3.Bucket(this, 'MemorySpacesBucket', {
      bucketName: getResourceName(config, 'memory-spaces'),
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

    this.table = new dynamodb.Table(this, 'MemorySpacesTable', {
      tableName: getResourceName(config, 'memory-spaces'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // OwnerIndex — reverse lookup of memory spaces by owner.
    this.table.addGlobalSecondaryIndex({
      indexName: 'OwnerIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // MemberIndex — reverse lookup of memory spaces by member.
    this.table.addGlobalSecondaryIndex({
      indexName: 'MemberIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
  }
}
