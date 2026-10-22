import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
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
import { managedKbEnvironmentTagValue } from '../managed-kb/kb-migration-construct';
import { ManagedKbRoleConstruct, managedKbMetricNamespace } from '../managed-kb/managed-kb-role-construct';
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
 * With the attachments knowledge base on, the first ingest in an environment
 * provisions the knowledge base inside this function: the saga waits up to
 * 300 s for ACTIVE (`KB_ACTIVE_WAIT_SECONDS`), measured 47–124 s, and a second
 * invocation that finds it mid-provisioning waits up to 360 s for it. Ten
 * minutes covers either, as it does for the conversation-index consumer.
 */
export const LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES_WITH_KB = 10;

/**
 * Event source mapping shape.
 *
 * - `BATCH_SIZE` 1: each message is a whole document, held in memory while it
 *   is prepared. A batch would multiply the memory and let one slow document
 *   hold its neighbours back.
 * - `MAX_CONCURRENCY` 5: uploads arrive one at a time per user; five in flight
 *   keeps a burst moving without letting it take the account's Lambda
 *   concurrency, and caps concurrent digest calls to the abstract model.
 * - `MAX_CONCURRENCY_WITH_KB` 2: with the attachments knowledge base on, this
 *   function also ingests and deletes knowledge-base documents, whose quotas
 *   (20 ingest/s, 10 delete/s, 10 concurrent operations) are per account and
 *   shared with every agent's uploads and the conversations consumer. Two in
 *   flight is the bound the conversation-index consumer uses for the same
 *   reason. It also caps concurrent preparations at two (each 2.5–12 s on dev).
 */
export const LARGE_DOCUMENT_PREPARE_ESM = {
  BATCH_SIZE: 1,
  MAX_CONCURRENCY: 5,
  MAX_CONCURRENCY_WITH_KB: 2,
} as const;

/** Daily attachments-KB reconcile (eviction, orphans, stuck statuses), 09:40 UTC. */
export const ATTACHMENT_KB_RECONCILE_SCHEDULE = { minute: '40', hour: '9' } as const;

export interface AttachmentKbProps {
  /** The assistants table: the KB_Record at `AST#attachments` and the per-user byte accounting beside it. */
  assistantsTable: dynamodb.ITable;
  /** Sessions metadata: a conversation's `lastMessageAt`, the eviction clock. */
  sessionsMetadataTable: dynamodb.ITable;
  managedKbRole: ManagedKbRoleConstruct;
}

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
  private readonly config: AppConfig;
  private readonly userFilesBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: LargeDocumentPrepareConstructProps) {
    super(scope, id);

    const { config, userFilesBucket, userFilesTable } = props;
    this.config = config;
    this.userFilesBucket = userFilesBucket;
    const timeoutMinutes = config.attachmentKb.enabled
      ? LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES_WITH_KB
      : LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES;

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
      visibilityTimeout: cdk.Duration.minutes(timeoutMinutes * 6),
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
      timeout: cdk.Duration.minutes(timeoutMinutes),
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
        maxConcurrency: config.attachmentKb.enabled
          ? LARGE_DOCUMENT_PREPARE_ESM.MAX_CONCURRENCY_WITH_KB
          : LARGE_DOCUMENT_PREPARE_ESM.MAX_CONCURRENCY,
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

  /**
   * The shared `attachments` knowledge base on this pipeline
   * (docs/specs/large-document-attachments.md §4E; `config.attachmentKb.enabled`).
   * Called by PlatformStack once the managed-KB role exists. The prepare Lambda
   * then also ingests a prepared original by its S3 location, removes the
   * document when the original is deleted, and evicts inactive documents daily
   * (`apis/shared/files/attachment_index.py`).
   *
   * Two resources, both rules targeting the existing queue (their send
   * permission is a statement on the queue's existing policy): S3 Object
   * Deleted for originals (never a sidecar or thumbnail), and the daily reconcile.
   * PlatformStack switches on the bucket's EventBridge delivery, since a
   * construct never mutates a bucket handed to it. Everything else is
   * statements on roles that already exist.
   */
  public enableAttachmentKb(props: AttachmentKbProps): void {
    const { config, userFilesBucket } = this;
    const { assistantsTable, sessionsMetadataTable, managedKbRole } = props;
    const fn = this.prepareLambda;

    fn.addEnvironment('ATTACHMENT_KB_ENABLED', 'true');
    fn.addEnvironment('LARGE_DOCUMENT_PREPARE_QUEUE_URL', this.queue.queueUrl);
    fn.addEnvironment('DYNAMODB_ASSISTANTS_TABLE_NAME', assistantsTable.tableName);
    fn.addEnvironment('DYNAMODB_SESSIONS_METADATA_TABLE_NAME', sessionsMetadataTable.tableName);
    fn.addEnvironment('MANAGED_KB_SERVICE_ROLE_ARN', managedKbRole.serviceRoleArn);
    fn.addEnvironment('MANAGED_KB_METRIC_NAMESPACE', managedKbMetricNamespace(config));
    // Tag contract values (apis/shared/kb_backend/tags.py): without them the
    // knowledge base would carry fallback tags that the reconciler and teardown,
    // which scope on them, would not recognise.
    fn.addEnvironment('MANAGED_KB_TAG_VALUE_PREFIX', config.projectPrefix);
    fn.addEnvironment('MANAGED_KB_TAG_VALUE_ENVIRONMENT', managedKbEnvironmentTagValue(config));

    // The KB_Record and the per-user byte accounting: one partition,
    // `AST#attachments`, and nothing else in the assistants table. The byte
    // reservation is a transaction of Put/Update/Delete on that partition.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'AttachmentKbRecordAndBytes',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
        resources: [assistantsTable.tableArn],
        conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['AST#attachments'] } },
      }),
    );
    // A conversation's lastMessageAt, looked up by session id.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'AttachmentKbSessionActivity',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query'],
        resources: [`${sessionsMetadataTable.tableArn}/index/SessionLookupIndex`],
      }),
    );
    // Delayed status checks and index retries go back onto this queue.
    this.queue.grantSendMessages(fn);

    // Bedrock: create (never delete) the shared knowledge base; ingest, list and
    // delete its documents.
    managedKbRole.grantCreation(fn.role!);
    managedKbRole.grantDirectIngestion(fn.role!);
    managedKbRole.grantDocumentReconciliation(fn.role!);

    // Bedrock reads each original by its S3 location, as the knowledge base's
    // service role. Originals only: never a sidecar or anything outside `user-files/`.
    managedKbRole.serviceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AttachmentKbUserFilesRead',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject'],
        resources: [userFilesBucket.arnForObjects(`${USER_FILES_PREFIX}*`)],
      }),
    );

    // Deleted originals (a user delete, a session cascade, lifecycle expiry):
    // the handler re-syncs the upload, finds it gone, and removes the document.
    // Sidecars and thumbnails are excluded here, because deleting one 500-page
    // PDF also deletes ~50 page shards, its text layer and its thumbnail, and
    // at batch size 1 that burst would queue ahead of other users'
    // preparations. The key cannot also carry a `user-files/` prefix matcher:
    // EventBridge allows one matcher per expression and ORs the expressions in
    // a field's array, so a prefix beside this exclusion would match every key
    // again. The bucket is the scope instead, since every upload is written
    // under `user-files/`, and the handler still rejects any key that is not an
    // original (`parse_original_key`).
    new events.Rule(this, 'AttachmentKbObjectDeletedRule', {
      ruleName: getResourceName(config, 'attachment-kb-object-deleted'),
      description: 'Deleted user-files originals -> large-document queue -> remove from the attachments KB',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Deleted'],
        detail: {
          bucket: { name: [userFilesBucket.bucketName] },
          object: { key: [{ 'anything-but': { wildcard: ['*/_derived/*', '*/_thumb.png'] } }] },
        },
      },
      targets: [new targets.SqsQueue(this.queue)],
    });

    new events.Rule(this, 'AttachmentKbReconcileSchedule', {
      ruleName: getResourceName(config, 'attachment-kb-reconcile'),
      description: 'Daily attachments-KB reconcile: evict inactive documents, remove orphans, settle statuses',
      schedule: events.Schedule.cron(ATTACHMENT_KB_RECONCILE_SCHEDULE),
      targets: [
        new targets.SqsQueue(this.queue, { message: events.RuleTargetInput.fromObject({ action: 'reconcile' }) }),
      ],
    });
  }
}
