import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { CONVERSATION_ARCHIVE_PREFIX } from '../data/conversation-archive-construct';
import { managedKbEnvironmentTagValue } from '../managed-kb/kb-migration-construct';
import {
  ManagedKbRoleConstruct,
  managedKbMetricNamespace,
} from '../managed-kb/managed-kb-role-construct';
import { logRetentionFor } from '../observability/log-retention';

/** The reserved app KB id (backend `apis/shared/kb_backend/reserved.py`). */
export const CONVERSATIONS_KB_ID = 'conversations';

/**
 * Event source mapping shape, exported so the test asserts the numbers the
 * spec argues for rather than restating them.
 *
 * - `BATCH_SIZE` 10: the managed KB document APIs take at most ten documents
 *   per call, so one batch is at most one ingest call and one delete call.
 * - `MAX_CONCURRENCY` 2 (the minimum Lambda allows): the document quotas —
 *   20 ingests/s, 10 deletes/s, 10 concurrent operations — are per ACCOUNT and
 *   shared with every agent's document uploads. Two invocations, each making
 *   one or two calls, cannot get near them.
 * - `BATCHING_WINDOW` 5 s: a session delete raises one event per turn in the
 *   same instant; waiting a few seconds turns them into one call. Indexing is
 *   background work and nobody waits on it.
 */
export const CONVERSATION_INDEX_ESM = {
  BATCH_SIZE: 10,
  MAX_CONCURRENCY: 2,
  BATCHING_WINDOW_SECONDS: 5,
} as const;

/**
 * Function timeout. Covers the one slow path: the first ingest of an
 * environment provisions the shared knowledge base (47–124 s measured to
 * ACTIVE, plus the data source), and a concurrent batch waits up to six
 * minutes for it (`PROVISIONING_WAIT_SECONDS` in the consumer).
 */
export const CONVERSATION_INDEX_TIMEOUT_MINUTES = 10;

/** Receives before a message dead-letters. With the visibility timeout below,
 * a message that never succeeds reaches the DLQ after about three hours. */
export const CONVERSATION_INDEX_MAX_RECEIVE_COUNT = 3;

export interface ConversationIndexConstructProps {
  config: AppConfig;
  /** The per-turn archive (`ConversationArchiveConstruct`); its EventBridge
   * delivery is switched on by the parent stack, not here. */
  archiveBucket: s3.IBucket;
  /** RAG assistants table — the shared KB's KB_Record lives at
   * `AST#conversations / KB#conversations`. */
  assistantsTable: dynamodb.ITable;
  managedKbRole: ManagedKbRoleConstruct;
}

/**
 * ConversationIndexConstruct — keeps the conversation-search Managed
 * Knowledge Base in step with the conversation archive
 * (docs/specs/conversation-search.md §4, PR-2b).
 *
 *   archive bucket ──S3 Object Created / Object Deleted──▶ EventBridge
 *     ──two rules, `conversations/` keys──▶ SQS ──batches of 10, ≤2 at once──▶
 *     consumer Lambda ──▶ IngestKnowledgeBaseDocuments / DeleteKnowledgeBaseDocuments
 *
 * WHY A QUEUE, when the kb-migration consumer is invoked by EventBridge
 * directly: batching. A session delete is one `DeleteObjects` that raises an
 * *Object Deleted* per turn at once, and lifecycle expiry arrives in daily
 * bursts. Direct invocation would make each its own Lambda and its own
 * one-document API call, at unbounded concurrency, against document quotas
 * that are per account and shared with users uploading documents to their
 * agents. The queue turns ten events into one call and caps this consumer at
 * two calls in flight. It also gives a longer retry runway than Lambda's
 * hard limit of two async retries, which matters while the shared knowledge
 * base is being provisioned on the first ingest.
 *
 * DARK BY DEFAULT: both rules are created DISABLED unless
 * `config.conversationIndex.enabled`, and the consumer checks the same flag
 * (CONVERSATION_INDEX_ENABLED) before touching anything.
 *
 * The knowledge base itself is NOT CDK: the consumer creates it lazily on the
 * first ingest, like every other managed knowledge base, and records it on
 * the KB_Record that the kb-migration reconciler joins against.
 *
 * SSM publication (consumed by the backend workflow's code-deploy step):
 *   /{prefix}/conversation-index/consumer-function-name
 */
export class ConversationIndexConstruct extends Construct {
  public readonly consumerLambda: lambda.DockerImageFunction;
  public readonly queue: sqs.Queue;
  public readonly deadLetterQueue: sqs.Queue;
  public readonly objectCreatedRule: events.Rule;
  public readonly objectDeletedRule: events.Rule;

  constructor(scope: Construct, id: string, props: ConversationIndexConstructProps) {
    super(scope, id);

    const { config, archiveBucket, assistantsTable, managedKbRole } = props;
    const enabled = config.conversationIndex.enabled;

    const bootstrapDir = path.resolve(
      __dirname,
      '..',
      '..',
      '..',
      'bootstrap-assets',
      'conversation-index',
    );

    // ── Queues ──
    // One DLQ for both failure points: messages the consumer gave up on
    // (redrive below) and events EventBridge could not deliver to the queue.
    this.deadLetterQueue = new sqs.Queue(this, 'ConversationIndexDlq', {
      queueName: getResourceName(config, 'conversation-index-dlq'),
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.queue = new sqs.Queue(this, 'ConversationIndexQueue', {
      queueName: getResourceName(config, 'conversation-index'),
      // AWS's guidance for an SQS event source is at least six times the
      // function timeout, so a batch is never redelivered while a slow
      // (provisioning) invocation still holds it.
      visibilityTimeout: cdk.Duration.minutes(CONVERSATION_INDEX_TIMEOUT_MINUTES * 6),
      retentionPeriod: cdk.Duration.days(4),
      // SSE-SQS, not KMS: EventBridge can deliver to an SSE-SQS queue without
      // a key policy, and the payload is an S3 key, not message text.
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      deadLetterQueue: {
        queue: this.deadLetterQueue,
        maxReceiveCount: CONVERSATION_INDEX_MAX_RECEIVE_COUNT,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Consumer ──
    const logGroup = new logs.LogGroup(this, 'ConversationIndexConsumerLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.consumerLambda = new lambda.DockerImageFunction(this, 'ConversationIndexConsumerLambda', {
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.conversation_index.consumer.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.minutes(CONVERSATION_INDEX_TIMEOUT_MINUTES),
      // Ten ≤16 KB objects and two control-plane calls per batch.
      memorySize: 512,
      logGroup,
      environment: {
        CONVERSATION_INDEX_ENABLED: enabled ? 'true' : 'false',
        CONVERSATION_ARCHIVE_BUCKET_NAME: archiveBucket.bucketName,
        DYNAMODB_ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
        MANAGED_KB_SERVICE_ROLE_ARN: managedKbRole.serviceRoleArn,
        MANAGED_KB_METRIC_NAMESPACE: managedKbMetricNamespace(config),
        // Tag contract values (apis/shared/kb_backend/tags.py). Without them
        // the knowledge base would carry the fallback tags and the reconciler
        // and teardown, which scope on them, would not recognise it.
        MANAGED_KB_TAG_VALUE_PREFIX: config.projectPrefix,
        MANAGED_KB_TAG_VALUE_ENVIRONMENT: managedKbEnvironmentTagValue(config),
      },
      description:
        'Conversation-search index consumer - ingests archived turns into, and deletes them from, the shared conversations knowledge base',
    });

    this.consumerLambda.addEventSource(
      new lambdaEventSources.SqsEventSource(this.queue, {
        batchSize: CONVERSATION_INDEX_ESM.BATCH_SIZE,
        maxBatchingWindow: cdk.Duration.seconds(CONVERSATION_INDEX_ESM.BATCHING_WINDOW_SECONDS),
        maxConcurrency: CONVERSATION_INDEX_ESM.MAX_CONCURRENCY,
        // One failed turn retries alone instead of with its nine neighbours.
        reportBatchItemFailures: true,
      }),
    );

    // ── IAM ──
    // The archive: read objects under `conversations/` only. ListBucket is on
    // the bucket ARN (it cannot be narrowed by key), and it is what makes a
    // missing object a 404 rather than a 403 — the consumer deletes the
    // document on a 404 and treats a 403 as a fault, so without it a delete
    // could never be told apart from a permissions error.
    this.consumerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ConversationIndexArchiveRead',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject'],
        resources: [archiveBucket.arnForObjects(`${CONVERSATION_ARCHIVE_PREFIX}*`)],
      }),
    );
    this.consumerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ConversationIndexArchiveList',
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListBucket'],
        resources: [archiveBucket.bucketArn],
      }),
    );

    // The KB_Record: its own partition and nothing else in the assistants
    // table. Get to read it, Put to create it, Update to attach the AWS ids.
    this.consumerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ConversationKbRecord',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        resources: [assistantsTable.tableArn],
        conditions: {
          'ForAllValues:StringEquals': {
            'dynamodb:LeadingKeys': [`AST#${CONVERSATIONS_KB_ID}`],
          },
        },
      }),
    );

    // Bedrock: create (never delete) the shared knowledge base, and ingest
    // and delete its documents.
    managedKbRole.grantCreation(this.consumerLambda.role!);
    managedKbRole.grantDirectIngestion(this.consumerLambda.role!);

    // ECR pull so the workflow's `update-function-code --image-uri` can swap
    // in the real image. Same shape as kb-migration; GetAuthorizationToken is
    // not scopeable to a repository.
    this.consumerLambda.addToRolePolicy(
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

    // ── Rules ──
    // *Object Deleted* covers both a user deleting a session (DeleteObjects)
    // and lifecycle expiry (`reason: "Lifecycle Expiration"`): the bucket is
    // unversioned, so an expiry is a permanent delete and raises the same
    // event. Separate rules rather than one with two detail types so either
    // can be disabled on its own during an incident.
    const ruleFor = (ruleId: string, detailType: string, what: string) => {
      const rule = new events.Rule(this, ruleId, {
        enabled,
        description: `Conversation index — archive ${what} → index queue`,
        eventPattern: {
          source: ['aws.s3'],
          detailType: [detailType],
          detail: {
            bucket: { name: [archiveBucket.bucketName] },
            object: { key: events.Match.prefix(CONVERSATION_ARCHIVE_PREFIX) },
          },
        },
      });
      rule.addTarget(
        new targets.SqsQueue(this.queue, { deadLetterQueue: this.deadLetterQueue }),
      );
      return rule;
    };
    this.objectCreatedRule = ruleFor('ConversationArchiveObjectCreatedRule', 'Object Created', 'turn written');
    this.objectDeletedRule = ruleFor('ConversationArchiveObjectDeletedRule', 'Object Deleted', 'turn deleted or expired');

    // ── SSM: generated function name for the code-deploy step ──
    new ssm.StringParameter(this, 'ConsumerFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/conversation-index/consumer-function-name`,
      stringValue: this.consumerLambda.functionName,
      description:
        'Conversation-index consumer Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
