import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { ALARM_PERIOD, AlarmFactory } from '../observability/alarm-factory';
import { logRetentionFor } from '../observability/log-retention';

/** Key prefix of every upload in the user-files bucket. */
export const USER_FILES_PREFIX = 'user-files/';

/**
 * Function sizing, from a measurement (PR-1 description). Preparing a 45 MB,
 * 1,000-page PDF peaked at 193 MB RSS; a 25 MB, 2,000-page dense-text PDF at
 * 226 MB, the worst of four fixtures. 1,024 MB is about 4.5x that peak, for
 * real-world PDFs with embedded fonts and heavier content streams. The timeout
 * is far above the measured 0.5–3.7 s (local arm64; Lambda's share of a vCPU at
 * this size is lower) because a slow document should finish, not be retried.
 */
export const LARGE_DOCUMENT_PREPARE_MEMORY_MB = 1024;
export const LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES = 5;

/**
 * Event source mapping shape.
 *
 * - `BATCH_SIZE` 1: each message is a whole document, held in memory while it
 *   is prepared. A batch would multiply the memory and let one slow document
 *   hold its neighbours back.
 * - `MAX_CONCURRENCY` 5: uploads arrive one at a time per user; five in flight
 *   keeps a burst moving without letting it take the account's Lambda
 *   concurrency, and caps concurrent digest calls to the abstract model.
 */
export const LARGE_DOCUMENT_PREPARE_ESM = {
  BATCH_SIZE: 1,
  MAX_CONCURRENCY: 5,
} as const;

/**
 * Receives before a message dead-letters. The handler records `failed` on the
 * last one when an AWS error persists, so the dead-letter queue only ever holds
 * crashes (a time-out or out of memory). Passed to the function as
 * LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT so the two cannot disagree.
 */
export const LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT = 3;

/** Nova Micro, the digest's abstract model (`DOCUMENT_DIGEST_MODEL_ID` default). */
export const DIGEST_MODEL_ID = 'amazon.nova-micro-v1:0';

export interface LargeDocumentPrepareConstructProps {
  config: AppConfig;
  /** The user-files bucket: originals under `user-files/`, sidecars under each upload's `_derived/`. */
  userFilesBucket: s3.IBucket;
  /** The user-files table: the file row the preparation reads and completes. */
  userFilesTable: dynamodb.ITable;
  alarmTopic?: sns.ITopic;
}

/**
 * LargeDocumentPrepareConstruct: the preparation pipeline for reference-class
 * attachments (docs/specs/large-document-attachments.md §4C, PR-1).
 *
 *   app-api complete_upload ──{user_id, upload_id}──▶ prepare queue
 *     ──one message at a time, ≤5 at once──▶ prepare Lambda
 *     ──▶ user-files/{user}/{session}/{upload_id}/_derived/ (text.jsonl, page shards)
 *     ──▶ file row: digest + preparation = ready
 *
 * Composed into PlatformStack only while `largeDocumentAttachments.enabled`
 * (CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED), so a deployment that never opts in
 * gets no queue, no Lambda and no alarms. app-api receives the queue's URL and
 * a send grant through PlatformComputeRefs.
 *
 * Same platform-as-bootstrap pattern as memory-maintenance: `fromImageAsset`
 * points at the byte-stable `bootstrap-assets/large-document-prepare/` stub,
 * and the backend workflow deploys the real image
 * (backend/Dockerfile.large-document-prepare) out of band
 * (`deploy-image-lambda-one.sh large-document-prepare`).
 *
 * Alarms: function errors here, and the dead-letter queue not being empty
 * through LambdaAlarmsConstruct in PlatformStack. No throttle alarm: the event
 * source's `maxConcurrency` keeps this function from throttling itself, and an
 * account-wide concurrency shortage already trips every other function's.
 *
 * Resource budget (PlatformStack sits near CloudFormation's 500): 11 resources
 * while on, none while off. `large-document-prepare.test.ts` pins the number.
 *
 * SSM publication (consumed by the backend workflow's code-deploy step):
 *   /{prefix}/large-document-prepare/function-name
 */
export class LargeDocumentPrepareConstruct extends Construct {
  public readonly prepareLambda: lambda.DockerImageFunction;
  public readonly queue: sqs.Queue;
  public readonly deadLetterQueue: sqs.Queue;

  constructor(scope: Construct, id: string, props: LargeDocumentPrepareConstructProps) {
    super(scope, id);

    const { config, userFilesBucket, userFilesTable } = props;

    const bootstrapDir = path.resolve(
      __dirname,
      '..',
      '..',
      '..',
      'bootstrap-assets',
      'large-document-prepare',
    );

    // ── Queues ──
    // No enforceSSL here, unlike the queue below: only SQS's own redrive
    // writes to a dead-letter queue, and the queue policy it would add is one
    // more resource in a stack near CloudFormation's limit.
    this.deadLetterQueue = new sqs.Queue(this, 'LargeDocumentPrepareDlq', {
      queueName: getResourceName(config, 'large-document-prepare-dlq'),
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.queue = new sqs.Queue(this, 'LargeDocumentPrepareQueue', {
      queueName: getResourceName(config, 'large-document-prepare'),
      // AWS's guidance for an SQS event source: at least six times the
      // function timeout, so a message is never redelivered mid-preparation.
      visibilityTimeout: cdk.Duration.minutes(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES * 6),
      retentionPeriod: cdk.Duration.days(4),
      // The payload is two ids; SSE-SQS needs no key policy for app-api.
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.deadLetterQueue,
        maxReceiveCount: LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Function ──
    const logGroup = new logs.LogGroup(this, 'LargeDocumentPrepareLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.prepareLambda = new lambda.DockerImageFunction(this, 'LargeDocumentPrepareLambda', {
      // No functionName: CDK generates one; the deploy script resolves it via SSM.
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.minutes(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES),
      memorySize: LARGE_DOCUMENT_PREPARE_MEMORY_MB,
      logGroup,
      environment: {
        DYNAMODB_USER_FILES_TABLE_NAME: userFilesTable.tableName,
        S3_USER_FILES_BUCKET_NAME: userFilesBucket.bucketName,
        LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT: String(LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT),
        // The text layer numbers DOCX paragraphs the way document_read does on
        // the Runtime, which depends on this flag; keep the three in step.
        WORD_TRACKED_CHANGES_ENABLED: config.wordTrackedChanges.enabled ? 'true' : 'false',
      },
      description:
        'Large-document preparation - writes the text layer and page shards document_read reads for an attachment over the inline limit',
    });

    this.prepareLambda.addEventSource(
      new lambdaEventSources.SqsEventSource(this.queue, {
        batchSize: LARGE_DOCUMENT_PREPARE_ESM.BATCH_SIZE,
        maxConcurrency: LARGE_DOCUMENT_PREPARE_ESM.MAX_CONCURRENCY,
        reportBatchItemFailures: true,
      }),
    );

    // ── IAM ──
    // Read any original; write and delete only sidecars. A sidecar key is
    // always `user-files/{user}/{session}/{upload_id}/_derived/<name>`.
    this.prepareLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'UserFilesOriginalRead',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject'],
        resources: [userFilesBucket.arnForObjects(`${USER_FILES_PREFIX}*`)],
      }),
    );
    this.prepareLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'UserFilesDerivedWrite',
        effect: iam.Effect.ALLOW,
        actions: ['s3:PutObject', 's3:DeleteObject'],
        resources: [userFilesBucket.arnForObjects(`${USER_FILES_PREFIX}*/_derived/*`)],
      }),
    );
    // Listing a `_derived/` prefix, to remove the sidecars of a file deleted
    // while it was being prepared or of a preparation that failed part-way.
    this.prepareLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'UserFilesDerivedList',
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListBucket'],
        resources: [userFilesBucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': [`${USER_FILES_PREFIX}*`] } },
      }),
    );
    // The file row: read it, and complete it with one conditional update.
    this.prepareLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'UserFilesRow',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'],
        resources: [userFilesTable.tableArn],
      }),
    );
    // The digest's abstract: one Converse call to Nova Micro through its US
    // cross-region inference profile, whose foundation model may serve from
    // any US region.
    this.prepareLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'DigestAbstractModel',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/${DIGEST_MODEL_ID}`,
          `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:inference-profile/us.${DIGEST_MODEL_ID}`,
        ],
      }),
    );
    // ECR pull so the workflow's `update-function-code --image-uri` can swap
    // in the real image. GetAuthorizationToken is not scopeable to a repository.
    this.prepareLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'EcrPullProjectImage',
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        resources: ['*'],
      }),
    );

    // ── Alarm ──
    // Errors are crashes only: the handler records a bad document as failed
    // and returns AWS errors as batch item failures, neither of which counts.
    new AlarmFactory(this, config, props.alarmTopic).alarm('LargeDocumentPrepareErrorAlarm', {
      name: 'lambda-large-document-prepare-errors',
      alarmDescription:
        'large-document-prepare Lambda crashed (timeout, out of memory or an unhandled error). '
        + 'The upload stays preparing until its message is redelivered or dead-letters.',
      metric: this.prepareLambda.metricErrors({ period: ALARM_PERIOD, statistic: 'Sum' }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── SSM: generated function name for the code-deploy step ──
    new ssm.StringParameter(this, 'FunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/large-document-prepare/function-name`,
      stringValue: this.prepareLambda.functionName,
      description:
        'Large-document prepare Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
