import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { ManagedKbRoleConstruct, managedKbMetricNamespace } from './managed-kb-role-construct';

/**
 * Tag keys every runtime-created Managed_KB carries (Requirement 20.11).
 *
 * Defined here, in CDK, even though the tags are applied at runtime by
 * `CreateKnowledgeBase`, because THREE independent readers have to agree
 * on the spelling: the provisioner that writes them, the Reconciler's
 * tag-filtered `ListKnowledgeBases` (Requirement 14.1), and
 * `scripts/teardown/destroy.sh` (Requirement 20.8). A typo in any one of
 * them is silent — the Reconciler simply finds nothing and reports a
 * clean account while orphans accrue, and teardown leaves paying
 * resources behind. One definition, shipped to the runtime as
 * environment variables below, removes the opportunity to disagree.
 *
 * `OWNER_USER_ID` carries an OPAQUE identifier and never an email
 * address or other personally identifying value (Requirement 20.12).
 * Resource tags are visible in Cost Explorer, CloudTrail, Config and
 * every `ListTagsForResource` caller in the account, none of which is an
 * appropriate destination for user PII — and unlike a database column a
 * tag cannot be scrubbed retroactively from the audit trail it has
 * already been written into.
 */
export const MANAGED_KB_TAG_KEYS = {
  /** Project prefix, so two deployments in one account never collide. */
  PREFIX: 'ManagedKbPrefix',
  /** Deployment environment, so dev teardown cannot delete prod resources. */
  ENVIRONMENT: 'ManagedKbEnvironment',
  /** Platform-side App_KB_Id (== assistant_id in this phase). */
  APP_KB_ID: 'ManagedKbAppKbId',
  /** Opaque owner id. NEVER an email address or other PII. */
  OWNER_USER_ID: 'ManagedKbOwnerUserId',
} as const;

/**
 * Default account quota for Bedrock knowledge bases, and the fraction of
 * it worth alarming at (Requirement 12.13).
 *
 * 80%, not 100%: the quota is adjustable but a capacity increase takes
 * lead time, so the alarm has to fire while there is still headroom to
 * file the request. An alarm at the limit is a post-mortem.
 */
export const MANAGED_KB_ACCOUNT_QUOTA = 10_000;
export const MANAGED_KB_COUNT_ALARM_FRACTION = 0.8;

/** Metric names the alarms below watch. Published by backend task 14.1. */
export const MANAGED_KB_METRICS = {
  /** Fleet-wide managed storage, in GB. */
  STORAGE_GB: 'KbStorageGB',
  /** Number of managed knowledge bases in the account. */
  COUNT: 'KbCount',
  /** Rolled-up daily Knowledge-Base `usagetype` cost, in USD. */
  DAILY_COST_USD: 'KbDailyCostUsd',
  /** Managed knowledge bases in AWS with no KB_Record. */
  ORPHANS_FOUND: 'KbOrphansFound',
} as const;

/**
 * Resolve the environment tag value.
 *
 * `config.tags.Environment` is the canonical source (it is what
 * `applyStandardTags` stamps on every CDK-created resource, so the
 * runtime-created knowledge bases end up labelled consistently with
 * them). The fallback is not cosmetic: this value is a *filter* for the
 * Reconciler and for teardown, so an empty string would silently widen
 * both to every environment in the account. A deterministic fallback is
 * therefore mandatory rather than nice to have.
 */
export function managedKbEnvironmentTagValue(config: AppConfig): string {
  return config.tags?.Environment ?? (config.production ? 'prod' : 'nonprod');
}

export interface KbMigrationConstructProps {
  config: AppConfig;
  /**
   * RAG assistants table — KB_Records live here as
   * `PK=AST#{assistant_id}` / `SK=KB#{app_kb_id}`, and the dispatcher
   * sweeps them through the sparse `KbWorkIndex` GSI added in task 1.1.
   */
  assistantsTable: dynamodb.ITable;
  /**
   * RAG documents bucket. The worker re-ingests source bytes from their
   * existing keys (Requirement 15.4 — the user is never asked to
   * re-supply anything), and byte accounting sizes each document with an
   * S3 `HEAD` rather than trusting a client-reported value
   * (Requirement 12.3).
   */
  documentsBucket: s3.IBucket;
  /**
   * The shared Managed_KB service role construct from task 1.2. Passed
   * as a construct ref, NOT read back from
   * `/{prefix}/managed-kb/service-role-arn`: that parameter is published
   * by this same stack, and CloudFormation resolves
   * `AWS::SSM::Parameter::Value` template parameters before any of the
   * stack's resources exist, so a same-stack read is unsatisfiable on
   * first deploy.
   *
   * This construct is where the provisioning and direct-ingestion grants
   * defined in task 1.2 finally get attached — they were deliberately
   * left unattached there, waiting for these Lambda roles.
   */
  managedKbRole: ManagedKbRoleConstruct;
}

/**
 * KbMigrationConstruct — the Managed_KB migration control plane
 * (.kiro/specs/managed-kb-migration, tasks 2.1, 2.2, 2.4, 2.5).
 *
 * FOUR DockerImage Lambdas sharing ONE image
 * (`backend/Dockerfile.kb-migration`) with different
 * `ImageConfig.Command` overrides:
 *   - dispatcher — EventBridge tick; sweeps the sparse `KbWorkIndex`,
 *     applies a bounded per-tick dispatch limit, invokes the worker.
 *     No-ops entirely when the migration flag is off (Req 19.6).
 *   - worker — runs one knowledge base through
 *     `shadow → verify → promote → retain` under a lease (Req 15).
 *   - reconciler — daily tag-filtered `ListKnowledgeBases` joined
 *     against KB_Records (Req 14). Ships DISARMED: it reports intended
 *     deletions and deletes nothing.
 *   - ingestion consumer — durable, retryable replacement for the
 *     in-process `asyncio.ensure_future` orchestration, routing each
 *     uploaded document to the legacy pipeline or to Direct_Ingestion
 *     according to its knowledge base's engine (Req 10).
 *
 * ONE IMAGE, FOUR FUNCTIONS. Every function points
 * `fromImageAsset` at the SAME byte-stable `bootstrap-assets/kb-migration/`
 * directory, so CDK emits a single image asset and the platform deploy
 * pushes one image rather than four. The per-function difference is the
 * `cmd` override, which lands in `ImageConfig.Command` — function
 * *configuration*, not code. That distinction is what makes the
 * platform-as-bootstrap pattern work here: the backend workflow's
 * out-of-band `aws lambda update-function-code --image-uri` swaps the
 * image and never touches the command, and the override paths are valid
 * in both the stub and the real image.
 *
 * `backend/Dockerfile.kb-migration` DOES NOT EXIST YET, and that is
 * deliberate. CDK's only build input is the bootstrap directory above;
 * the real image is a *workflow* artefact, and it cannot be built before
 * the handler modules it COPYs exist (task groups 9, 10 and 13). Creating
 * it now would produce a Dockerfile that COPYs absent paths — unbuildable,
 * unregistered in `scripts/build/build-one.sh`, and unreferenced by any
 * `backend.yml` job. It arrives with the handlers, exactly as
 * `backend/Dockerfile.kb-sync` did, together with its build-one.sh case,
 * its build/deploy jobs, and its entries in the supply-chain
 * dockerfile-pinning and Lambda-import-closure tests.
 *
 * DARK BY DEFAULT. All three flags ship off (Req 19.5), and the
 * construct is inert in that state:
 *   - the dispatcher's rule is created DISABLED, and the dispatcher also
 *     gates on its own `MANAGED_KB_MIGRATION_ENABLED` env var, so an
 *     operator can dark-stop migration via CFN or a plain env flip;
 *   - the ingestion consumer's trigger rule is created DISABLED (see the
 *     long note on that rule — it must stay disabled while the legacy
 *     pipeline owns the S3 event, or every upload would be ingested
 *     twice);
 *   - the reconciler's rule is deliberately ENABLED. That is not an
 *     oversight. Requirement 14.7 makes report-only the initial deployed
 *     mode precisely so its judgement can be reviewed against weeks of
 *     real data before `reconcilerArmed` lets it delete anything.
 *     Report-only is read-only, so an enabled schedule changes nothing.
 *
 * Additive by construction: this construct creates only new resources.
 * The one thing it needs from the existing world — S3 `ObjectCreated`
 * events — it takes over EventBridge rather than by editing the
 * documents bucket's existing Lambda notification (see the rule's note).
 *
 * SSM publications (consumed by the backend workflow's code-deploy):
 *   /{prefix}/kb-migration/dispatcher-function-name
 *   /{prefix}/kb-migration/worker-function-name
 *   /{prefix}/kb-migration/reconciler-function-name
 *   /{prefix}/kb-migration/ingestion-consumer-function-name
 */
export class KbMigrationConstruct extends Construct {
  public readonly dispatcherLambda: lambda.DockerImageFunction;
  public readonly workerLambda: lambda.DockerImageFunction;
  public readonly reconcilerLambda: lambda.DockerImageFunction;
  public readonly ingestionConsumerLambda: lambda.DockerImageFunction;
  /** Async-invocation dead-letter queue for the ingestion consumer (Req 10.1). */
  public readonly ingestionConsumerDlq: sqs.Queue;
  public readonly dispatcherScheduleRule: events.Rule;
  public readonly reconcilerScheduleRule: events.Rule;
  /** Documents-bucket `Object Created` rule feeding the ingestion consumer. */
  public readonly documentsEventRule: events.Rule;

  constructor(scope: Construct, id: string, props: KbMigrationConstructProps) {
    super(scope, id);

    const { config, assistantsTable, documentsBucket, managedKbRole } = props;
    const { managedKb } = config;

    const bootstrapDir = path.resolve(
      __dirname,
      '..',
      '..',
      '..',
      'bootstrap-assets',
      'kb-migration',
    );

    // Flags and tag values shared by every function. Booleans are
    // stringified once here so all four functions cannot disagree, and
    // the tag values ride along so the runtime provisioner never has to
    // reconstruct them (Requirement 20.11).
    const sharedEnvironment: Record<string, string> = {
      DYNAMODB_ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
      S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: documentsBucket.bucketName,
      MANAGED_KB_SERVICE_ROLE_ARN: managedKbRole.serviceRoleArn,
      MANAGED_KB_METRIC_NAMESPACE: managedKbMetricNamespace(config),
      MANAGED_KB_NEW_DEFAULT: managedKb.newDefault ? 'true' : 'false',
      MANAGED_KB_MIGRATION_ENABLED: managedKb.migrationEnabled ? 'true' : 'false',
      MANAGED_KB_RECONCILER_ARMED: managedKb.reconcilerArmed ? 'true' : 'false',
      MANAGED_KB_PER_OWNER_DEFAULT_BYTES: String(managedKb.perOwnerDefaultBytes),
      MANAGED_KB_PER_OWNER_ELEVATED_BYTES: String(managedKb.perOwnerElevatedBytes),
      MANAGED_KB_PER_KB_CEILING_BYTES: String(managedKb.perKnowledgeBaseCeilingBytes),
      // Read by worker._retain_days(). The MANAGED_KB_ spelling was published and
      // read by nothing, so Requirement 15.11's configured window was silently
      // replaced by the code's 30-day floor.
      KB_MIGRATION_RETAIN_DAYS: String(managedKb.retentionWindowDays),
      // Tag contract (Requirement 20.11). Keys AND the two values that
      // are static at deploy time; `appKbId` and the opaque owner id are
      // per-knowledge-base and can only be supplied at CreateKnowledgeBase
      // time by the backend.
      MANAGED_KB_TAG_KEY_PREFIX: MANAGED_KB_TAG_KEYS.PREFIX,
      MANAGED_KB_TAG_KEY_ENVIRONMENT: MANAGED_KB_TAG_KEYS.ENVIRONMENT,
      MANAGED_KB_TAG_KEY_APP_KB_ID: MANAGED_KB_TAG_KEYS.APP_KB_ID,
      MANAGED_KB_TAG_KEY_OWNER_USER_ID: MANAGED_KB_TAG_KEYS.OWNER_USER_ID,
      MANAGED_KB_TAG_VALUE_PREFIX: config.projectPrefix,
      MANAGED_KB_TAG_VALUE_ENVIRONMENT: managedKbEnvironmentTagValue(config),
    };

    // ── Worker ──
    const workerLogGroup = new logs.LogGroup(this, 'KbMigrationWorkerLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.workerLambda = new lambda.DockerImageFunction(this, 'KbMigrationWorkerLambda', {
      // No functionName — CDK auto-generates; the deploy script resolves
      // it through the SSM parameter published at the bottom of this file.
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.kb_migration.worker.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // 15 min, Lambda's maximum. A 20-document knowledge base measured
      // ~3 min and 100 documents ~6.5 min, but a PDF-heavy corpus runs
      // 37-264 s PER DOCUMENT, so a single knowledge base can exceed the
      // deadline. The state machine is resumable under a lease precisely
      // so hitting the ceiling costs a re-dispatch, not a broken
      // migration.
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      logGroup: workerLogGroup,
      environment: sharedEnvironment,
      description:
        'Managed_KB migration worker - runs one knowledge base through shadow/verify/promote/retain under a lease',
    });

    // ── Dispatcher ──
    const dispatcherLogGroup = new logs.LogGroup(this, 'KbMigrationDispatcherLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.dispatcherLambda = new lambda.DockerImageFunction(this, 'KbMigrationDispatcherLambda', {
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.kb_migration.dispatcher.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Sweeps at most a bounded number of knowledge bases per tick
      // (Requirement 15.14) and async-invokes the worker; small and fast.
      timeout: cdk.Duration.minutes(2),
      memorySize: 512,
      logGroup: dispatcherLogGroup,
      environment: {
        ...sharedEnvironment,
        // The dispatcher reads KB_MIGRATION_WORKER_FUNCTION_NAME — matching the
        // house convention its siblings use (KB_SYNC_WORKER_FUNCTION_NAME,
        // SCHEDULED_RUNS_WORKER_FUNCTION_NAME). Named MANAGED_KB_* here, the
        // dispatcher raised `KB_MIGRATION_WORKER_FUNCTION_NAME is not set` on every
        // tick and no knowledge base could ever be migrated.
        KB_MIGRATION_WORKER_FUNCTION_NAME: this.workerLambda.functionName,
      },
      description:
        'Managed_KB migration dispatcher - sweeps the sparse KbWorkIndex on a schedule and invokes the worker',
    });

    // ── Reconciler ──
    const reconcilerLogGroup = new logs.LogGroup(this, 'KbMigrationReconcilerLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.reconcilerLambda = new lambda.DockerImageFunction(this, 'KbMigrationReconcilerLambda', {
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.kb_migration.reconciler.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Paginates ListKnowledgeBases across the whole account and joins
      // it against KB_Records; a bounded per-run action limit
      // (Requirement 14.8) keeps a single run finite.
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      logGroup: reconcilerLogGroup,
      environment: sharedEnvironment,
      description:
        'Managed_KB daily reconciler - joins tag-filtered ListKnowledgeBases against KB_Records (report-only until armed)',
    });

    // ── Ingestion consumer + its dead-letter queue (task 2.2) ──
    //
    // The DLQ is the "durable retry anchor" half of Requirement 10.1/10.7.
    // The trigger is asynchronous, so a function that exhausts its retries
    // has nowhere to report failure to: the caller is EventBridge, which
    // does not care. Without a DLQ the document simply never becomes
    // searchable and nobody finds out — the exact silent-failure mode this
    // Lambda exists to remove from the old in-process orchestration.
    this.ingestionConsumerDlq = new sqs.Queue(this, 'KbIngestionConsumerDlq', {
      queueName: getResourceName(config, 'kb-ingestion-consumer-dlq'),
      // Long enough that a failure over a holiday weekend is still there
      // to triage on the Tuesday. 14 days is the SQS maximum.
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const ingestionConsumerLogGroup = new logs.LogGroup(this, 'KbIngestionConsumerLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.ingestionConsumerLambda = new lambda.DockerImageFunction(this, 'KbIngestionConsumerLambda', {
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.kb_migration.ingestion_consumer.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Requirement 10.9 sets the floor at 300 s, and that floor is
      // measured, not guessed: §5.1 found a fixed ~68 s per-knowledge-base
      // warm-up plus a long tail to 264 s for a single 50 KiB PDF, and
      // this function polls until the document is ACTUALLY RETRIEVABLE
      // (Requirement 10.6) rather than merely reported indexed. 15 min —
      // Lambda's maximum — leaves headroom above the measured tail. A
      // timeout under 300 s would turn a slow-but-succeeding ingestion
      // into a dead-lettered document.
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      logGroup: ingestionConsumerLogGroup,
      // Async-invocation DLQ. `retryAttempts` is Lambda's own bounded
      // retry (Requirement 10.7) before the event lands in the queue.
      deadLetterQueue: this.ingestionConsumerDlq,
      retryAttempts: 2,
      environment: sharedEnvironment,
      description:
        'Managed_KB ingestion consumer - routes an uploaded document to the legacy pipeline or to Direct_Ingestion',
    });

    const allFunctions = [
      this.dispatcherLambda,
      this.workerLambda,
      this.reconcilerLambda,
      this.ingestionConsumerLambda,
    ];

    // ── IAM ──

    // KB_Records and the sparse work index live on the assistants table.
    // The dispatcher writes too: claiming a knowledge base for dispatch
    // is a conditional update, not a read.
    for (const fn of allFunctions) {
      assistantsTable.grantReadWriteData(fn);
    }

    // Source bytes for re-ingestion, and the S3 HEAD that byte accounting
    // sizes documents with (Requirement 12.3 — never a client-reported
    // size). Read-only: nothing here writes user documents.
    documentsBucket.grantRead(this.workerLambda);
    documentsBucket.grantRead(this.ingestionConsumerLambda);

    this.workerLambda.grantInvoke(this.dispatcherLambda);

    // ── Managed_KB grants from task 1.2 ──
    //
    // These are the callers the role construct was waiting for. Attached
    // via its public methods rather than re-declared inline, so the
    // confused-deputy conditions and the resource scopes have exactly one
    // definition and the assertions in managed-kb.test.ts remain the
    // single authority on their shape.
    //
    // Split by who actually needs what, so no function holds a permission
    // its job does not require:
    //   - provisioning CRUD + iam:PassRole → worker (creates the shadow
    //     knowledge base) and reconciler (deletes aged orphans, and needs
    //     ListKnowledgeBases to find them at all).
    //   - direct ingestion → worker (migration re-ingest) and ingestion
    //     consumer (interactive upload).
    //   - bedrock:Retrieve → worker (the `verify` canary retrieval,
    //     Requirement 15.7) and ingestion consumer (polling until
    //     actually retrievable, Requirement 10.6).
    // The dispatcher gets NONE of them: it reads an index and invokes the
    // worker, and touches Bedrock not at all.
    managedKbRole.grantProvisioning(this.workerLambda.role!);
    managedKbRole.grantProvisioning(this.reconcilerLambda.role!);
    managedKbRole.grantDirectIngestion(this.workerLambda.role!);
    managedKbRole.grantDirectIngestion(this.ingestionConsumerLambda.role!);
    managedKbRole.grantRetrieval(this.workerLambda.role!);
    managedKbRole.grantRetrieval(this.ingestionConsumerLambda.role!);

    // The dispatcher receives none of the three grants above, each of
    // which carries its own namespace-conditioned PutMetricData
    // statement, so it needs one of its own to emit dispatch metrics.
    // Distinct SID (CloudFormation rejects duplicate SIDs within one
    // policy document) and the namespace comes from the shared helper, so
    // there is still exactly one definition of where our metrics go.
    this.dispatcherLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ManagedKbDispatchMetrics',
        effect: iam.Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        // PutMetricData has no resource-level permissions; the namespace
        // condition is the only available scope.
        resources: ['*'],
        conditions: {
          StringEquals: { 'cloudwatch:namespace': managedKbMetricNamespace(config) },
        },
      }),
    );

    // ECR pull on the project's image repo so the workflow's
    // `update-function-code --image-uri` can swap to the real image.
    // Same rationale and shape as kb-sync and rag-ingestion;
    // GetAuthorizationToken is not scopeable to a repository.
    const ecrPullStatement = new iam.PolicyStatement({
      sid: 'EcrPullProjectImage',
      effect: iam.Effect.ALLOW,
      actions: [
        'ecr:GetAuthorizationToken',
        'ecr:BatchCheckLayerAvailability',
        'ecr:GetDownloadUrlForLayer',
        'ecr:BatchGetImage',
      ],
      resources: ['*'],
    });
    for (const fn of allFunctions) {
      fn.addToRolePolicy(ecrPullStatement);
    }

    // ── Schedules ──

    // Migration dispatch. Disabled rule = no migration work can start,
    // whatever else is configured, because this rule is the only
    // initiator (Requirement 19.6). Eligible knowledge bases just
    // accumulate sparse KbWorkIndex keys as inert data.
    this.dispatcherScheduleRule = new events.Rule(this, 'KbMigrationDispatcherSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      enabled: managedKb.migrationEnabled,
      description:
        'Managed_KB migration dispatcher tick — the only initiator of migration work',
    });
    this.dispatcherScheduleRule.addTarget(new targets.LambdaFunction(this.dispatcherLambda));

    // Daily reconciliation. ENABLED regardless of the flags, and that is
    // the requirement rather than an omission: Requirement 14.7 makes
    // report-only the INITIAL DEPLOYED MODE so the Reconciler's judgement
    // can be audited against real data for weeks before `reconcilerArmed`
    // permits a single delete. Gating the schedule on a flag would mean
    // the report-only period never happens, and the first thing the
    // Reconciler ever did would be to delete. Report-only is read-only,
    // so an enabled schedule changes no state.
    this.reconcilerScheduleRule = new events.Rule(this, 'KbMigrationReconcilerSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.days(1)),
      enabled: true,
      description:
        'Managed_KB daily reconciler tick — runs report-only until MANAGED_KB_RECONCILER_ARMED is set',
    });
    this.reconcilerScheduleRule.addTarget(new targets.LambdaFunction(this.reconcilerLambda));

    // ── Documents-bucket ObjectCreated trigger (task 2.2) ──
    //
    // WHY EVENTBRIDGE AND NOT A SECOND BUCKET NOTIFICATION.
    // The documents bucket already has an `s3:ObjectCreated:*`
    // notification with prefix `assistants/` pointing at the legacy
    // rag-ingestion Lambda. S3 REJECTS a notification configuration that
    // has overlapping prefixes for the same event type — "your
    // notification configurations that use Filter can't define filtering
    // rules with overlapping prefixes ... for the same event types", and
    // an identical prefix is the maximally overlapping case. So a second
    // `addEventNotification(OBJECT_CREATED, ..., { prefix: 'assistants/' })`
    // does not coexist with the first; it makes
    // PutBucketNotificationConfiguration fail and takes the deploy down.
    // The only ways to put two Lambdas behind one S3 event are to replace
    // the existing destination with a fan-out (which mutates live
    // behaviour and would drop the current subscription) or to route
    // through EventBridge, which is a SEPARATE field of the bucket's
    // notification configuration and therefore purely additive to the
    // existing LambdaFunctionConfigurations.
    //
    // WHY THE RULE SHIPS DISABLED.
    // While the legacy pipeline still owns the S3 event, having both
    // triggers live would hand every upload to both consumers — and this
    // consumer routes a legacy document straight back into the legacy
    // pipeline, so the document would be ingested TWICE
    // (Requirement 10.5). It is enabled once either managed creation or a
    // migration is on, at which point routing is what we want. Note also
    // that a bucket notification has no enabled/disabled switch, so
    // EventBridge is the only trigger primitive that can be dark-shipped
    // at all — a second reason it is the right one here.
    //
    // `enableEventBridgeNotification()` on the bucket is called by the
    // parent stack, not here: this construct never mutates a bucket
    // passed in as a prop, the same discipline the rag-ingestion
    // construct documents for `addEventNotification`.
    this.documentsEventRule = new events.Rule(this, 'KbIngestionDocumentsRule', {
      enabled: managedKb.newDefault || managedKb.migrationEnabled,
      description:
        'Managed_KB ingestion consumer trigger — documents-bucket Object Created, routed via EventBridge so the legacy notification is untouched',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [documentsBucket.bucketName] },
          // Same key scope as the legacy notification's prefix filter.
          object: { key: events.Match.prefix('assistants/') },
        },
      },
    });
    this.documentsEventRule.addTarget(
      new targets.LambdaFunction(this.ingestionConsumerLambda, {
        deadLetterQueue: this.ingestionConsumerDlq,
      }),
    );

    // ── Account-level alarms (task 2.5, Requirement 12.13) ──
    //
    // Per-owner byte caps bound ONE user. These bound the fleet, and the
    // gap they cover is the whole reason they exist: ~$169/month expected
    // at measured behaviour versus ~$15,000/month permitted by the
    // per-owner caps alone. A per-owner cap cannot see a thousand users
    // each behaving legitimately.
    //
    // Every alarm uses TreatMissingData.NOT_BREACHING, matching the
    // kb-sync, scheduled-runs and prompt-cache observability constructs.
    // The metrics below do not exist until backend task 14.1 publishes
    // them, and an alarm that screamed INSUFFICIENT_DATA from the moment
    // it was created would be routed to a mute rule within a week and
    // then be worth nothing when it finally had something to say.
    const namespace = managedKbMetricNamespace(config);

    new cloudwatch.Alarm(this, 'ManagedKbTotalStorageAlarm', {
      alarmName: getResourceName(config, 'managed-kb-total-storage'),
      alarmDescription:
        'Fleet-wide managed knowledge base storage exceeded the configured GB threshold',
      metric: new cloudwatch.Metric({
        namespace,
        metricName: MANAGED_KB_METRICS.STORAGE_GB,
        statistic: cloudwatch.Stats.MAXIMUM,
        period: cdk.Duration.hours(1),
      }),
      threshold: managedKb.storageAlarmGb,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cloudwatch.Alarm(this, 'ManagedKbCountAlarm', {
      alarmName: getResourceName(config, 'managed-kb-count'),
      alarmDescription:
        'Managed knowledge base count reached 80% of the 10,000 per-account quota — a quota increase takes lead time',
      metric: new cloudwatch.Metric({
        namespace,
        metricName: MANAGED_KB_METRICS.COUNT,
        statistic: cloudwatch.Stats.MAXIMUM,
        period: cdk.Duration.hours(1),
      }),
      threshold: MANAGED_KB_ACCOUNT_QUOTA * MANAGED_KB_COUNT_ALARM_FRACTION,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Daily Knowledge-Base `usagetype` cost.
    //
    // Deliberately watches a metric WE publish, not AWS/Billing.
    // AWS/Billing lives only in us-east-1 and a CloudWatch alarm cannot
    // read a metric from another region, so a billing-namespace alarm
    // here would evaluate against a metric that does not exist in this
    // region and sit at INSUFFICIENT_DATA forever. The rollup also has to
    // filter on `usagetype`: Managed KB bills under
    // `AmazonBedrockAgentCore`, so keying on `AmazonBedrock` misses it
    // entirely and keying on the service code alone blends it into the
    // AgentCore Runtime memory line (Requirement 22.7).
    new cloudwatch.Alarm(this, 'ManagedKbDailyCostAlarm', {
      alarmName: getResourceName(config, 'managed-kb-daily-cost'),
      alarmDescription:
        'Daily Knowledge-Base usagetype cost exceeded the configured USD threshold',
      metric: new cloudwatch.Metric({
        namespace,
        metricName: MANAGED_KB_METRICS.DAILY_COST_USD,
        statistic: cloudwatch.Stats.MAXIMUM,
        period: cdk.Duration.days(1),
      }),
      threshold: managedKb.dailyCostAlarmUsd,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Sustained non-zero orphan count. Threshold 0 with
    // GREATER_THAN_THRESHOLD, so ANY orphan counts — but three
    // consecutive daily runs, because one orphan on one day is a create
    // that crashed and will be cleaned up, whereas the same finding three
    // days running means the delete saga is leaking and every leaked
    // knowledge base is still billing.
    new cloudwatch.Alarm(this, 'ManagedKbOrphansAlarm', {
      alarmName: getResourceName(config, 'managed-kb-orphans'),
      alarmDescription:
        'Reconciler reported orphaned managed knowledge bases on three consecutive runs — the delete saga is leaking',
      metric: new cloudwatch.Metric({
        namespace,
        metricName: MANAGED_KB_METRICS.ORPHANS_FOUND,
        statistic: cloudwatch.Stats.MAXIMUM,
        period: cdk.Duration.days(1),
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── SSM: generated function names for the code-deploy step ──
    const functionNameParameters: Array<[string, string, lambda.IFunction]> = [
      ['DispatcherFunctionNameParameter', 'dispatcher', this.dispatcherLambda],
      ['WorkerFunctionNameParameter', 'worker', this.workerLambda],
      ['ReconcilerFunctionNameParameter', 'reconciler', this.reconcilerLambda],
      ['IngestionConsumerFunctionNameParameter', 'ingestion-consumer', this.ingestionConsumerLambda],
    ];
    for (const [logicalId, slug, fn] of functionNameParameters) {
      new ssm.StringParameter(this, logicalId, {
        parameterName: `/${config.projectPrefix}/kb-migration/${slug}-function-name`,
        stringValue: fn.functionName,
        description: `Managed_KB migration ${slug} Lambda function name (consumed by backend workflow code-deploy step)`,
        tier: ssm.ParameterTier.STANDARD,
      });
    }
  }
}
