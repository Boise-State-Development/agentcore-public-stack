import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { grantManagedKbDocumentDeletion } from '../managed-kb/managed-kb-role-construct';

export interface KbSyncConstructProps {
  config: AppConfig;
  /** RAG assistants table — sync policies live on it (SYNCPOL# items + DueSyncIndex). */
  assistantsTable: dynamodb.ITable;
  /** RAG documents bucket — the worker re-stages changed source bytes here. */
  documentsBucket: s3.IBucket;
  /** OAuth providers table — the worker resolves connector config from it. */
  oauthProvidersTable: dynamodb.ITable;
  /**
   * Shared platform workload identity name. Vault entries are keyed by
   * workload identity — a different workload sees an empty vault, so this
   * MUST be the same identity app-api/inference-api use.
   */
  workloadIdentityName: string;
}

/**
 * KbSyncConstruct — scheduled re-index of assistant knowledge-base
 * sources (docs/specs/assistant-kb-sync.md).
 *
 * Two DockerImage Lambdas sharing ONE image (backend/Dockerfile.kb-sync)
 * with different ImageConfig.Command overrides:
 *   - dispatcher — EventBridge rate(15 minutes) tick; sweeps the sparse
 *     DueSyncIndex, applies the runaway guards, async-invokes the worker.
 *   - worker — executes a single policy's sync run (stub until PR-3).
 *
 * Same platform-as-bootstrap pattern as rag-ingestion: `fromImageAsset`
 * points at the byte-stable `bootstrap-assets/kb-sync/` stub; the real
 * image is deployed out-of-band by the backend workflow
 * (`deploy-image-lambda-one.sh kb-sync-dispatcher|kb-sync-worker`).
 * The command overrides are function *configuration*, so out-of-band
 * `update-function-code --image-uri` swaps never touch them — the
 * override module paths are valid in both the stub and real images.
 *
 * Runaway posture: the EventBridge rule is the ONLY initiator of sync
 * work, and it's created disabled unless `config.kbSync.enabled`. The
 * dispatcher additionally gates on the KB_SYNC_ENABLED env var, so an
 * operator can dark-stop the feature either via CFN (rule) or a plain
 * env flip (function config), whichever is faster.
 *
 * SSM publications (consumed by the backend workflow's code-deploy):
 *   /{prefix}/kb-sync/dispatcher-function-name
 *   /{prefix}/kb-sync/worker-function-name
 */
export class KbSyncConstruct extends Construct {
  public readonly dispatcherLambda: lambda.DockerImageFunction;
  public readonly workerLambda: lambda.DockerImageFunction;
  public readonly scheduleRule: events.Rule;

  constructor(scope: Construct, id: string, props: KbSyncConstructProps) {
    super(scope, id);

    const { config, assistantsTable, documentsBucket, oauthProvidersTable, workloadIdentityName } = props;

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
      'kb-sync',
    );

    const workerLogGroup = new logs.LogGroup(this, 'KbSyncWorkerLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.workerLambda = new lambda.DockerImageFunction(this, 'KbSyncWorkerLambda', {
      // No functionName — CDK auto-generates; deploy script resolves via SSM.
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.kb_sync.worker.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Worker fetches source bytes and stages to S3; modest memory (no ML
      // deps). 15 min is Lambda's max; re-crawls run with a 13-minute
      // crawler budget (worker passes budget_seconds) so finalize + miss
      // accounting always fit before the Lambda deadline.
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      logGroup: workerLogGroup,
      environment: {
        DYNAMODB_ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
        DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME: oauthProvidersTable.tableName,
        S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: documentsBucket.bucketName,
        AGENTCORE_RUNTIME_WORKLOAD_NAME: workloadIdentityName,
        AGENTCORE_LOCAL_OAUTH_CALLBACK_URL: oauthCallbackUrl,
        KB_SYNC_ENABLED: config.kbSync.enabled ? 'true' : 'false',
      },
      description:
        'KB sync worker - re-fetches a synced knowledge-base source and stages changed content for re-ingestion',
    });

    const dispatcherLogGroup = new logs.LogGroup(this, 'KbSyncDispatcherLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.dispatcherLambda = new lambda.DockerImageFunction(this, 'KbSyncDispatcherLambda', {
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir, {
        cmd: ['apis.app_api.kb_sync.dispatcher.lambda_handler'],
      }),
      architecture: lambda.Architecture.ARM_64,
      // Sweeps at most KB_SYNC_DISPATCH_LIMIT policies per tick; small.
      timeout: cdk.Duration.minutes(2),
      memorySize: 512,
      logGroup: dispatcherLogGroup,
      environment: {
        DYNAMODB_ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
        KB_SYNC_ENABLED: config.kbSync.enabled ? 'true' : 'false',
        KB_SYNC_WORKER_FUNCTION_NAME: this.workerLambda.functionName,
      },
      description:
        'KB sync dispatcher - sweeps due sync policies on an EventBridge schedule, applies runaway guards, invokes the worker',
    });

    // IAM — both functions do policy bookkeeping on the assistants table.
    assistantsTable.grantReadWriteData(this.dispatcherLambda);
    assistantsTable.grantReadWriteData(this.workerLambda);
    this.workerLambda.grantInvoke(this.dispatcherLambda);

    // Worker: read connector config, stage changed bytes for re-ingestion.
    oauthProvidersTable.grantReadData(this.workerLambda);
    documentsBucket.grantPut(this.workerLambda);

    // Worker: remove a vanished document from a promoted knowledge base.
    // `kb_sync/worker.py` soft-deletes a document whose upstream source is
    // gone and then calls `cleanup_service.cleanup_document_resources`, which
    // deletes from the managed knowledge base when the assistant has been
    // promoted. Same grant and same reasoning as the app-api task role: the
    // sync worker removes documents, it never writes a corpus, so this is the
    // deletion grant and not `grantDirectIngestion`.
    grantManagedKbDocumentDeletion(config, this.workerLambda.role!);

    // Worker: retrieve the policy creator's stored 3LO token from the
    // AgentCore Identity vault with no live user session. Mirrors app-api's
    // AgentCoreWorkloadIdentityAccess statement (app-api-iam-grants.ts)
    // minus consent-completion and provider CRUD — a background fetcher
    // only ever reads tokens.
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
    // backing secret. Read-only mirror of the app-api / inference-api grants
    // (a background fetcher never registers providers, so no write lifecycle).
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

    // Best-effort custom metrics (KBSync namespace). PutMetricData
    // doesn't support resource scoping; condition on the namespace.
    const metricsStatement = new iam.PolicyStatement({
      sid: 'KbSyncPutMetrics',
      effect: iam.Effect.ALLOW,
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': 'KBSync' } },
    });
    this.dispatcherLambda.addToRolePolicy(metricsStatement);
    this.workerLambda.addToRolePolicy(metricsStatement);

    // ECR pull on the project's kb-sync repo so `update-function-code
    // --image-uri` can swap to the real image (same rationale/pattern
    // as the rag-ingestion construct; GetAuthorizationToken is
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
    // (policies accumulate as plain data until it's enabled).
    this.scheduleRule = new events.Rule(this, 'KbSyncSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      enabled: config.kbSync.enabled,
      description: 'KB sync dispatcher tick — the only initiator of scheduled re-index work',
    });
    this.scheduleRule.addTarget(new targets.LambdaFunction(this.dispatcherLambda));

    // Error visibility (no SNS wiring in this stack yet; alarms are
    // dashboard/console signals).
    new cloudwatch.Alarm(this, 'KbSyncDispatcherErrorAlarm', {
      alarmName: `${config.projectPrefix}-kb-sync-dispatcher-errors`,
      metric: this.dispatcherLambda.metricErrors({ period: cdk.Duration.minutes(15) }),
      threshold: 1,
      evaluationPeriods: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, 'KbSyncWorkerErrorAlarm', {
      alarmName: `${config.projectPrefix}-kb-sync-worker-errors`,
      metric: this.workerLambda.metricErrors({ period: cdk.Duration.minutes(15) }),
      threshold: 3,
      evaluationPeriods: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new ssm.StringParameter(this, 'DispatcherFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/kb-sync/dispatcher-function-name`,
      stringValue: this.dispatcherLambda.functionName,
      description: 'KB sync dispatcher Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'WorkerFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/kb-sync/worker-function-name`,
      stringValue: this.workerLambda.functionName,
      description: 'KB sync worker Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
