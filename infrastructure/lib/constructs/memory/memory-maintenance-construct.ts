import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory } from '../observability/alarm-factory';
import { logRetentionFor } from '../observability/log-retention';

/**
 * Worker timeout: Lambda's maximum. A run plans files a few at a time and
 * stops starting new ones with about two and a half minutes left (the
 * runner's deadline margin), so a run over a large space ends cleanly.
 */
export const MEMORY_MAINTENANCE_TIMEOUT_MINUTES = 15;

export interface MemoryMaintenanceConstructProps {
  config: AppConfig;
  /** Memory spaces table: the run row and snapshot, proposals, provenance. */
  memorySpacesTable: dynamodb.ITable;
  /** Memory spaces bucket: the worker reads files, and writes a member's tidied files (2.6b). */
  memorySpacesBucket: s3.IBucket;
  /** Projects table: project META and members, the cost rollup, the inbox. */
  projectsTable: dynamodb.ITable;
  alarmTopic?: sns.ITopic;
}

/**
 * MemoryMaintenanceConstruct: the worker for Shared Projects 2.6
 * (docs/specs/shared-projects.md §4.6).
 *
 * One DockerImage Lambda (backend/Dockerfile.memory-maintenance). app-api
 * async-invokes it with `{spaceId, runId}` when an editor starts a run; it
 * snapshots the space, asks the model for compaction changes one file at a
 * time and verifies them deterministically. In a project's shared memory it
 * writes compaction proposals for an editor to review (2.6a); in a member's
 * own memory it saves the changes, which the member can undo (2.6b). Saves
 * only ever add content-addressed objects, so the bucket grant is read and
 * put, never delete.
 *
 * Nothing schedules it yet: the only initiator is a person, through app-api
 * (which needs the projects feature on). Phase 3.5 adds a weekly sweeper.
 * The worker itself never reads PROJECTS_ENABLED: app-api checked the
 * requester's access when the run was queued, and the worker re-checks the
 * project and membership from the projects table.
 *
 * Same platform-as-bootstrap pattern as scheduled-runs: `fromImageAsset`
 * points at the byte-stable `bootstrap-assets/memory-maintenance/` stub, and
 * the backend workflow deploys the real image out of band
 * (`deploy-image-lambda-one.sh memory-maintenance-worker`).
 *
 * SSM publication (consumed by the backend workflow's code-deploy step):
 *   /{prefix}/memory-maintenance/worker-function-name
 */
export class MemoryMaintenanceConstruct extends Construct {
  public readonly workerLambda: lambda.DockerImageFunction;

  constructor(scope: Construct, id: string, props: MemoryMaintenanceConstructProps) {
    super(scope, id);

    const { config, memorySpacesTable, memorySpacesBucket, projectsTable } = props;

    const bootstrapDir = path.resolve(
      __dirname,
      '..',
      '..',
      '..',
      'bootstrap-assets',
      'memory-maintenance',
    );

    const logGroup = new logs.LogGroup(this, 'MemoryMaintenanceWorkerLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.workerLambda = new lambda.DockerImageFunction(this, 'MemoryMaintenanceWorkerLambda', {
      // No functionName: CDK generates one; the deploy script resolves it via SSM.
      code: lambda.DockerImageCode.fromImageAsset(bootstrapDir),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.minutes(MEMORY_MAINTENANCE_TIMEOUT_MINUTES),
      memorySize: 1024,
      // Lambda retries a failed async invoke twice. The runner already ignores
      // a run that isn't queued; no retries keeps a crash from being replayed.
      retryAttempts: 0,
      logGroup,
      environment: {
        DYNAMODB_MEMORY_SPACES_TABLE_NAME: memorySpacesTable.tableName,
        S3_MEMORY_SPACES_BUCKET_NAME: memorySpacesBucket.bucketName,
        DYNAMODB_PROJECTS_TABLE_NAME: projectsTable.tableName,
        // Content lint (Shared Projects 2.7): a run's merges, split descriptions
        // and pointer items are checked like any save.
        MEMORY_LINT_MODE: config.memoryLint.mode,
        ...(config.memoryLint.sensitivePatterns
          ? { MEMORY_SENSITIVE_PATTERNS: config.memoryLint.sensitivePatterns }
          : {}),
      },
      description:
        'Memory maintenance worker - plans and verifies compaction changes to project memory, and proposes or applies them',
    });

    // Run rows, snapshots, proposals and provenance reads, and the lock release.
    memorySpacesTable.grantReadWriteData(this.workerLambda);
    // Files are read through their content-addressed keys. A member's tidied
    // file is a new object (2.6b); replaced objects stay, since version rows
    // reference them, so the worker never deletes.
    memorySpacesBucket.grantRead(this.workerLambda);
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'MemorySpacesObjectPut',
        effect: iam.Effect.ALLOW,
        actions: ['s3:PutObject'],
        resources: [memorySpacesBucket.arnForObjects('spaces/*')],
      }),
    );
    // Project META and members, the COST# rollup (UpdateItem) and the inbox.
    projectsTable.grantReadWriteData(this.workerLambda);

    // The planner's Converse call, and CountTokens on each proposed file (the
    // save checks). Same resources as app-api and the runtime: inference
    // profiles are account-scoped ARNs in this region, and the profile's
    // underlying foundation models may live in any region.
    this.workerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'BedrockMaintenanceModel',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:CountTokens'],
        resources: [
          'arn:aws:bedrock:*::foundation-model/*',
          `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:*`,
        ],
      }),
    );

    // ECR pull on the project's memory-maintenance repo so
    // `update-function-code --image-uri` can swap in the real image (same
    // rationale as the other image Lambdas; GetAuthorizationToken is unscopeable).
    this.workerLambda.addToRolePolicy(
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

    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    alarms.alarm('MemoryMaintenanceWorkerErrorAlarm', {
      name: 'memory-maintenance-worker-errors',
      metric: this.workerLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new ssm.StringParameter(this, 'WorkerFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/memory-maintenance/worker-function-name`,
      stringValue: this.workerLambda.functionName,
      description: 'Memory maintenance worker Lambda function name (consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
