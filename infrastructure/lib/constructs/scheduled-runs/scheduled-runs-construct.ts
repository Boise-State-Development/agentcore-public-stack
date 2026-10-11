import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory } from '../observability/alarm-factory';
import { logRetentionFor } from '../observability/log-retention';

export interface ScheduledRunsConstructProps {
  config: AppConfig;
  /** Sessions-metadata table — schedules (SCHEDPROMPT# items + sparse
   * DueScheduleIndex), the run-audit trail (RUN# items), and delivered
   * session rows all live here. */
  sessionsMetadataTable: dynamodb.ITable;
  /** BFF sessions table — headless-grant records
   * (PK=HEADLESS-GRANT#{id}) live here behind the sparse
   * HeadlessGrantUserIndex GSI. */
  bffSessionsTable: dynamodb.ITable;
  /** Projects table — a schedule that runs in a project (Shared Projects 3.2):
   * the dispatcher reads the project and the creator's member row before each
   * run, and both Lambdas write the project's run rows and pause notices. */
  projectsTable: dynamodb.ITable;
  /** Delivering a scheduled run to its project (Shared Projects 3.2b): the worker
   * snapshots the run's conversation (AgentCore Memory events, read only), pins its
   * artifacts (the artifacts table's SessionIndex), writes the share (row, S3 body),
   * and records it on the project's trail (audit log). A schedule that publishes
   * its outputs (3.2d) also writes the run's artifact share rows, and deletes the
   * previous run's, in the artifacts table. */
  agentCoreMemoryArn: string;
  agentCoreMemoryId: string;
  sharedConversationsTable: dynamodb.ITable;
  sharedConversationsBucket: s3.IBucket;
  artifactsTable: dynamodb.ITable;
  auditLogTable: dynamodb.ITable;
  /** BFF Cognito app client — the worker mints a per-owner access token
   * via REFRESH_TOKEN_AUTH against this (confidential) client. */
  bffAppClient: cognito.IUserPoolClient;
  bffAppClientSecret: secretsmanager.ISecret;
  /**
   * Shared platform workload identity name. Vault entries are keyed by
   * workload identity — a different workload sees an empty vault, so this
   * MUST be the same identity app-api/inference-api use.
   */
  workloadIdentityName: string;
  /** AgentCore Runtime `/invocations` data-plane base URL — the worker's
   * only HTTP dependency (via run_agent_headless). */
  inferenceApiRuntimeEndpointUrl: string;
  cognitoRegion: string;
  alarmTopic?: sns.ITopic;
}

/**
 * ScheduledRunsConstruct — the scheduler engine (B2) for the F3 scheduled
 * trigger (docs/specs/scheduled-runs-phase-b-brief.md,
 * docs/specs/scheduled-agent-runs.md §4/§7). Mirrors KbSyncConstruct
 * (infrastructure/lib/constructs/kb-sync/kb-sync-construct.ts) closely.
 *
 * Two DockerImage Lambdas sharing ONE image
 * (backend/Dockerfile.scheduled-runs) with different ImageConfig.Command
 * overrides:
 *   - dispatcher — EventBridge rate(5 minutes) tick; sweeps the sparse
 *     DueScheduleIndex, applies the runaway guard, conditionally re-arms,
 *     async-invokes the worker.
 *   - worker — resolves the schedule, calls run_agent_headless as the
 *     owning user, records the outcome (and pauses on terminal failure
 *     modes — reauth_required / oauth_required / repeated_failures).
 *
 * Same platform-as-bootstrap pattern as kb-sync: `fromImageAsset` points
 * at the byte-stable `bootstrap-assets/scheduled-runs/` stub; the real
 * image is deployed out-of-band by the backend workflow
 * (`deploy-image-lambda-one.sh scheduled-runs-dispatcher|scheduled-runs-worker`).
 * The command overrides are function *configuration*, so out-of-band
 * `update-function-code --image-uri` swaps never touch them.
 *
 * Runaway posture: the EventBridge rule is the ONLY initiator of
 * scheduled-run work, and it's created disabled unless
 * `config.scheduledRuns.enabled`. The dispatcher additionally gates on
 * the SCHEDULED_RUNS_ENABLED env var, so an operator can dark-stop the
 * feature either via CFN (rule) or a plain env flip (function config),
 * whichever is faster.
 *
 * SSM publications (consumed by the backend workflow's code-deploy):
 *   /{prefix}/scheduled-runs/dispatcher-function-name
 *   /{prefix}/scheduled-runs/worker-function-name
 */
export class ScheduledRunsConstruct extends Construct {
  public readonly dispatcherLambda: lambda.DockerImageFunction;
  public readonly workerLambda: lambda.DockerImageFunction;
  public readonly scheduleRule: events.Rule;

  constructor(scope: Construct, id: string, props: ScheduledRunsConstructProps) {
    super(scope, id);

    const {
      config,
      sessionsMetadataTable,
      bffSessionsTable,
      projectsTable,
      agentCoreMemoryArn,
      agentCoreMemoryId,
      sharedConversationsTable,
      sharedConversationsBucket,
      artifactsTable,
      auditLogTable,
      bffAppClient,
      bffAppClientSecret,
      workloadIdentityName,
      inferenceApiRuntimeEndpointUrl,
      cognitoRegion,
    } = props;

    // Same value app-api uses (app-api-environment.ts) — the vault requires
    // a registered return URL even for non-interactive token reads.
    const oauthCallbackUrl = config.domainName
      ? `https://${config.domainName}/oauth-complete`
      : 'http://localhost:4200/oauth-complete';

    const bootstrapDir = path.resolve(
      __dirname,
      '..',
      '..',
      '..',
      'bootstrap-assets',
      'scheduled-runs',
    );

    const sharedEnvironment = {
      DYNAMODB_SESSIONS_METADATA_TABLE_NAME: sessionsMetadataTable.tableName,
      BFF_SESSIONS_TABLE_NAME: bffSessionsTable.tableName,
      COGNITO_BFF_APP_CLIENT_ID: bffAppClient.userPoolClientId,
      COGNITO_BFF_APP_CLIENT_SECRET_ARN: bffAppClientSecret.secretArn,
      COGNITO_REGION: cognitoRegion,
      INFERENCE_API_URL: inferenceApiRuntimeEndpointUrl,
      AGENTCORE_RUNTIME_WORKLOAD_NAME: workloadIdentityName,
      AGENTCORE_LOCAL_OAUTH_CALLBACK_URL: oauthCallbackUrl,
      SCHEDULED_RUNS_ENABLED: config.scheduledRuns.enabled ? 'true' : 'false',
      DYNAMODB_PROJECTS_TABLE_NAME: projectsTable.tableName,
      PROJECTS_ENABLED: config.projects.enabled ? 'true' : 'false',
    };

    const workerLogGroup = new logs.LogGroup(this, 'ScheduledRunsWorkerLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.workerLambda = new lambda.DockerImageFunction(this, 'ScheduledRunsWorkerLambda', {
      // No functionName — CDK auto-generates; deploy script resolves via SSM.
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['worker.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Worker drains a full agent turn's SSE stream through the harness
      // (run_agent_headless's own budget is 300s); give it rag-ingestion's
      // ceiling + headroom for retries/bookkeeping. Same memory class as
      // rag-ingestion (no heavy ML deps here, but the httpx stream +
      // json accumulation benefit from headroom over the dispatcher's).
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      logGroup: workerLogGroup,
      environment: {
        ...sharedEnvironment,
        SCHEDULED_RUNS_MAX_FAILURES: '3',
        // Delivery (3.2b): what a share of the run reads and writes.
        AGENTCORE_MEMORY_ID: agentCoreMemoryId,
        SHARED_CONVERSATIONS_TABLE_NAME: sharedConversationsTable.tableName,
        SHARED_CONVERSATIONS_BUCKET_NAME: sharedConversationsBucket.bucketName,
        DYNAMODB_ARTIFACTS_TABLE_NAME: artifactsTable.tableName,
        DYNAMODB_AUDIT_LOG_TABLE_NAME: auditLogTable.tableName,
      },
      description:
        'Scheduled-runs worker - executes one schedule\'s headless agent run via run_agent_headless',
    });

    const dispatcherLogGroup = new logs.LogGroup(this, 'ScheduledRunsDispatcherLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.dispatcherLambda = new lambda.DockerImageFunction(this, 'ScheduledRunsDispatcherLambda', {
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['dispatcher.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Sweeps at most SCHEDULED_RUNS_DISPATCH_LIMIT schedules per tick; small.
      timeout: cdk.Duration.minutes(2),
      memorySize: 512,
      logGroup: dispatcherLogGroup,
      environment: {
        DYNAMODB_SESSIONS_METADATA_TABLE_NAME: sessionsMetadataTable.tableName,
        SCHEDULED_RUNS_ENABLED: config.scheduledRuns.enabled ? 'true' : 'false',
        SCHEDULED_RUNS_WORKER_FUNCTION_NAME: this.workerLambda.functionName,
        DYNAMODB_PROJECTS_TABLE_NAME: projectsTable.tableName,
        PROJECTS_ENABLED: config.projects.enabled ? 'true' : 'false',
      },
      description:
        'Scheduled-runs dispatcher - sweeps due schedules on an EventBridge tick, applies the runaway guard, invokes the worker',
    });

    // IAM — dispatcher only touches the schedule/due-index bookkeeping on
    // the sessions-metadata table (list_due_schedules, rearm_schedule,
    // set_schedule_state).
    sessionsMetadataTable.grantReadWriteData(this.dispatcherLambda);
    // A project schedule's standing (Shared Projects 3.2): the project and the
    // creator's member row are read (and the member's userId back-filled, as any
    // role check does), and a pause writes a run row and the inbox notices.
    projectsTable.grantReadWriteData(this.dispatcherLambda);
    this.workerLambda.grantInvoke(this.dispatcherLambda);

    // Worker: schedule bookkeeping + run-audit trail + delivered session
    // rows, all on the same adjacency-list table (governance.py's
    // RunAuditRecorder and the harness's ensure_session_metadata_exists /
    // update_session_title both write here too).
    sessionsMetadataTable.grantReadWriteData(this.workerLambda);
    // A project schedule's run rows, and the notice when the worker pauses one.
    projectsTable.grantReadWriteData(this.workerLambda);

    // Delivering a run to its project (Shared Projects 3.2b). The snapshot reads
    // the run's Memory events, never writes them: the session already exists, so
    // the session manager only reads it. The share is a row plus an S3 body under
    // shares/, and an older delivery is unshared (row and body deleted).
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ScheduledRunSnapshotMemoryRead',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock-agentcore:ListEvents', 'bedrock-agentcore:GetEvent'],
        resources: [agentCoreMemoryArn],
      }),
    );
    sharedConversationsTable.grantReadWriteData(this.workerLambda);
    // The snapshot store checks for an existing body (HeadObject, which IAM
    // authorizes as s3:GetObject) before it writes one. Without s3:ListBucket a
    // missing key answers 403 rather than 404, which the store rightly treats as
    // a real error, so the listing is granted too, limited to shares/.
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ScheduledRunShareBodies',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
        resources: [sharedConversationsBucket.arnForObjects('shares/*')],
      }),
    );
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ScheduledRunShareBodiesList',
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListBucket'],
        resources: [sharedConversationsBucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': ['shares/*'] } },
      }),
    );
    // Read: the run's artifacts. Write: the artifact shares a schedule that
    // publishes its outputs makes and retires (Shared Projects 3.2d).
    artifactsTable.grantReadWriteData(this.workerLambda);
    auditLogTable.grantWriteData(this.workerLambda);

    // Worker: resolve + touch the caller's headless-grant record
    // (HeadlessGrantService — get_active_grant / persist_rotated_refresh_token
    // / record_use), keyed by the sparse HeadlessGrantUserIndex GSI.
    bffSessionsTable.grantReadWriteData(this.workerLambda);

    // Worker: CognitoRefreshClient's REFRESH_TOKEN_AUTH exchange. Cognito's
    // InitiateAuth is an unauthenticated data-plane API (no IAM action
    // exists for it — gated by the app client itself), so no IAM
    // statement is needed for the Cognito call; only the secret read that
    // resolves SECRET_HASH is an IAM action.
    bffAppClientSecret.grantRead(this.workerLambda);

    // Worker: retrieve the schedule owner's stored 3LO connector tokens
    // from the AgentCore Identity vault with no live user session (the
    // headless run resolves enabled_tools' connector tokens exactly as an
    // attended chat turn would). Mirrors app-api's
    // AgentCoreWorkloadIdentityAccess statement (app-api-iam-grants.ts)
    // minus consent-completion and provider CRUD — a background runner
    // only ever reads tokens. Same statement shape as KbSyncConstruct.
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'AgentCoreVaultTokenRead',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
          'bedrock-agentcore:GetResourceOauth2Token',
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:token-vault/*`,
          `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:token-vault/*/oauth2credentialprovider/*`,
          `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/*`,
          `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/*/workload-identity/*`,
        ],
      }),
    );

    // GetResourceOauth2Token reads the vaulted refresh token *through* a
    // Secrets Manager secret the vault auto-creates per provider
    // (bedrock-agentcore-identity!default/oauth2/<id>) — the bedrock-agentcore
    // action alone is not enough, the caller must also be able to read that
    // backing secret. Read-only mirror of the app-api / inference-api / kb-sync
    // grants (a background runner never registers providers, so no write
    // lifecycle).
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'AgentCoreIdentityOAuthSecrets',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
        resources: [
          `arn:aws:secretsmanager:${config.awsRegion}:${config.awsAccount}:secret:bedrock-agentcore-identity!default/oauth2/*`,
        ],
      }),
    );

    // Worker: the AgentCore Runtime data-plane invocation itself is
    // authenticated via the minted Cognito bearer over HTTPS (see
    // apis.shared.harness.runner), not SigV4/invoke_agent_runtime — so no
    // separate `bedrock-agentcore:InvokeAgentRuntime` IAM action is needed
    // here (matches the "Run now" route's app-api role, which also grants
    // no such action).

    // Best-effort custom metrics (ScheduledRuns namespace). PutMetricData
    // doesn't support resource scoping; condition on the namespace.
    const metricsStatement = new iam.PolicyStatement({
      sid: 'ScheduledRunsPutMetrics',
      effect: iam.Effect.ALLOW,
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': 'ScheduledRuns' } },
    });
    this.dispatcherLambda.addToRolePolicy(metricsStatement);
    this.workerLambda.addToRolePolicy(metricsStatement);

    // ECR pull on the project's scheduled-runs repo so `update-function-code
    // --image-uri` can swap to the real image (same rationale/pattern
    // as the rag-ingestion/kb-sync constructs; GetAuthorizationToken is
    // unscopeable).
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
    this.dispatcherLambda.addToRolePolicy(ecrPullStatement);
    this.workerLambda.addToRolePolicy(ecrPullStatement);

    // The single trigger. Disabled rule = the whole feature is inert
    // (schedules accumulate as plain data until it's enabled).
    this.scheduleRule = new events.Rule(this, 'ScheduledRunsSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      enabled: config.scheduledRuns.enabled,
      description: 'Scheduled-runs dispatcher tick — the only initiator of scheduled agent-run work',
    });
    this.scheduleRule.addTarget(new targets.LambdaFunction(this.dispatcherLambda));

    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    alarms.alarm('ScheduledRunsDispatcherErrorAlarm', {
      name: 'scheduled-runs-dispatcher-errors',
      metric: this.dispatcherLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.alarm('ScheduledRunsWorkerErrorAlarm', {
      name: 'scheduled-runs-worker-errors',
      metric: this.workerLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
      threshold: 3,
      evaluationPeriods: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new ssm.StringParameter(this, 'DispatcherFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/scheduled-runs/dispatcher-function-name`,
      stringValue: this.dispatcherLambda.functionName,
      description: 'Scheduled-runs dispatcher Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'WorkerFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/scheduled-runs/worker-function-name`,
      stringValue: this.workerLambda.functionName,
      description: 'Scheduled-runs worker Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
