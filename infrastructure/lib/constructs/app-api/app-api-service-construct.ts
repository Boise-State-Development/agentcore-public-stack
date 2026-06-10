import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, buildCorsOrigins } from '../../config';
import { resolveAppApiParams, buildAppApiEnvironment } from './app-api-environment';
import { PlatformComputeRefs } from '../platform-compute-refs';
import { grantAppApiPermissions } from './app-api-iam-grants';

export interface AppApiServiceConstructProps {
  config: AppConfig;
  /**
   * Typed bundle of every PlatformStack resource ref the App API
   * needs at synth time. Replaces the previous in-construct
   * `valueForStringParameter` calls — same-stack SSM reads cause
   * a CFN parameter-resolution deadlock on first deploy.
   */
  refs: PlatformComputeRefs;
  /**
   * AgentCore Memory ARN. Sourced directly from the sibling
   * InferenceAgentCoreConstruct in PlatformStack, not from SSM,
   * because publisher and consumer share a stack.
   */
  agentCoreMemoryArn: string;
  /**
   * AgentCore Memory ID. Same-stack ref via InferenceAgentCoreConstruct;
   * used as the App API container's `MEMORY_ID` env var.
   */
  agentCoreMemoryId: string;
  /**
   * Bedrock AgentCore Runtime endpoint URL. Same-stack ref via
   * InferenceAgentCoreConstruct; used as the App API container's
   * `INFERENCE_API_URL` env var.
   */
  inferenceApiRuntimeEndpointUrl: string;
  /**
   * Artifacts iframe origin URL (https://artifacts.{domain}). Same-stack
   * ref via ArtifactsDistributionConstruct; used as the App API
   * container's `ARTIFACTS_ORIGIN` env var.
   */
  artifactsOrigin: string;
  /**
   * SageMaker fine-tuning execution role ARN. Same-stack ref via
   * SageMakerExecutionRoleConstruct; consumed as both an env var
   * and an IAM PassRole grant on the App API task role.
   */
  sagemakerExecutionRoleArn: string;
  /**
   * SageMaker fine-tuning security group ID. Same-stack ref via
   * SageMakerExecutionRoleConstruct.
   */
  sagemakerSecurityGroupId: string;
  /**
   * Comma-separated VPC private subnet IDs the SageMaker training
   * jobs run in. Same-stack ref derived from PlatformStack's VPC.
   */
  sagemakerPrivateSubnetIds: string;
}

/**
 * AppApiServiceConstruct — ECS Fargate service for the App API.
 *
 * Provisions:
 *   - ECS task definition + container (env vars via app-api-environment.ts)
 *   - IAM grants on the task role (via app-api-iam-grants.ts)
 *   - ALB target group + listener rule
 *   - Fargate service with auto-scaling
 *   - Security group (ingress from ALB on :8000)
 *   - Assistants DynamoDB table (local to this construct)
 *   - Artifacts env vars (S3 bucket, DDB table, origin, render token)
 *
 * All cross-stack values are resolved from SSM at synth time via
 * `resolveAppApiSsmParams()`. The container reads them as plain env
 * vars at runtime.
 */
export class AppApiServiceConstruct extends Construct {
  public readonly ecsService: ecs.FargateService;

  constructor(scope: Construct, id: string, props: AppApiServiceConstructProps) {
    super(scope, id);

    const { config } = props;

    // ── Resolve all values from typed refs (replaces SSM reads) ──
    const params = resolveAppApiParams(props.refs, {
      memoryId: props.agentCoreMemoryId,
      inferenceApiRuntimeEndpointUrl: props.inferenceApiRuntimeEndpointUrl,
    });

    // ── Network resources (typed refs from PlatformStack) ──
    const vpc = props.refs.vpc;
    const albSecurityGroup = props.refs.albSecurityGroup;
    const albListener = props.refs.albListener;
    const ecsCluster = props.refs.ecsCluster;

    // ── Security group ──
    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'AppEcsSecurityGroup', {
      vpc,
      securityGroupName: getResourceName(config, 'app-ecs-sg'),
      description: 'Security group for App API ECS Fargate tasks',
      allowAllOutbound: true,
    });
    ecsSecurityGroup.addIngressRule(
      albSecurityGroup, ec2.Port.tcp(8000),
      'Allow traffic from ALB to App API tasks',
    );

    // ── Task definition ──
    const taskDefinition = new ecs.FargateTaskDefinition(this, 'AppApiTaskDefinition', {
      family: getResourceName(config, 'app-api-task'),
      cpu: config.appApi.cpu,
      memoryLimitMiB: config.appApi.memory,
    });

    // Auto-generated log group name (no `logGroupName` set) so a
    // failed-deploy orphan can't collide with a redeploy.
    const logGroup = new logs.LogGroup(this, 'AppApiLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const ecrRepository = ecr.Repository.fromRepositoryName(
      this, 'AppApiRepository', getResourceName(config, 'app-api'),
    );

    // ── Bootstrap container image + SSM-resolved live image ──
    // The task definition's container image is read from an SSM
    // parameter at CFN deploy time, NOT baked into the synthesized
    // template. This means: when CFN re-registers the task def
    // (any property change — env var, CPU, memory, role, etc.),
    // the new revision picks up whatever URI is currently in SSM,
    // which is the latest image the build pipeline pushed. The
    // bootstrap stub is never reverted onto a live service.
    //
    // Bootstrap responsibility:
    //   - First-deploy seed lives in scripts/stack-bootstrap/
    //     seed-image-tags.sh, which runs before `cdk deploy` in
    //     scripts/platform/deploy.sh. It pushes the bootstrap image
    //     below to the cdk-assets ECR repo (via cdk-assets publish)
    //     and writes its URI to SSM if the parameter doesn't exist.
    //   - Subsequent runs: the build pipeline (backend.yml's
    //     deploy-app-api-code → deploy-ecs-service-one.sh) overwrites
    //     the SSM tag with the per-service ECR URI on every push.
    //
    // The DockerImageAsset is kept (not directly referenced by the
    // ContainerDefinition anymore) so cdk-assets continues to
    // publish it for the seed step. The CfnOutput exposes its
    // assetHash so the seed script can construct the cdk-assets URI
    // without needing to parse Fn::Sub from the template.
    const bootstrapImage = new ecr_assets.DockerImageAsset(this, 'AppApiBootstrap', {
      directory: path.resolve(
        __dirname, '..', '..', '..', 'bootstrap-assets', 'app-api',
      ),
      platform: ecr_assets.Platform.LINUX_AMD64,
    });
    new cdk.CfnOutput(this, 'AppApiBootstrapImageHash', {
      description: 'cdk-assets image tag for the app-api bootstrap container. Consumed by scripts/stack-bootstrap/seed-image-tags.sh on first deploy.',
      value: bootstrapImage.assetHash,
    });

    const appApiImageTagSsmPath = `/${config.projectPrefix}/app-api/image-tag`;
    const appApiImageUri = ssm.StringParameter.valueForStringParameter(
      this, appApiImageTagSsmPath,
    );

    // ── Container environment ──
    const environment = buildAppApiEnvironment(config, params);

    // Artifacts env vars (always-on). All values now sourced from
    // typed PlatformStack refs — same-stack SSM reads removed.
    environment['S3_ARTIFACTS_BUCKET_NAME'] = props.refs.artifactsContentBucket.bucketName;
    environment['DYNAMODB_ARTIFACTS_TABLE_NAME'] = props.refs.artifactsTable.tableName;
    environment['ARTIFACTS_ORIGIN'] = props.artifactsOrigin;
    environment['ARTIFACTS_RENDER_TOKEN_SECRET_ARN'] = props.refs.artifactRenderTokenSecret.secretArn;

    // Skill reference-file bucket (admin-managed Skills, PR-4). Read by
    // apis/shared/skills/resource_store.py via S3_SKILL_RESOURCES_BUCKET_NAME.
    environment['S3_SKILL_RESOURCES_BUCKET_NAME'] = props.refs.skillResourcesBucket.bucketName;

    // Fine-tuning env vars (always-on). Names verified against
    // backend/src/apis/app_api/fine_tuning/* to match the exact env
    // var names Python reads via os.environ.get(...).
    environment['DYNAMODB_FINE_TUNING_JOBS_TABLE_NAME'] = props.refs.fineTuningJobsTable.tableName;
    environment['DYNAMODB_FINE_TUNING_ACCESS_TABLE_NAME'] = props.refs.fineTuningAccessTable.tableName;
    environment['S3_FINE_TUNING_BUCKET_NAME'] = props.refs.fineTuningDataBucket.bucketName;
    environment['SAGEMAKER_EXECUTION_ROLE_ARN'] = props.sagemakerExecutionRoleArn;
    environment['SAGEMAKER_SECURITY_GROUP_ID'] = props.sagemakerSecurityGroupId;
    environment['SAGEMAKER_SUBNET_IDS'] = props.sagemakerPrivateSubnetIds;

    // ── Container definition ──
    const container = taskDefinition.addContainer('AppApiContainer', {
      containerName: 'app-api',
      image: ecs.ContainerImage.fromRegistry(appApiImageUri),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'app-api', logGroup }),
      environment,
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
      healthCheck: {
        command: ['CMD-SHELL', "python3 -c 'import urllib.request,sys; urllib.request.urlopen(\"http://localhost:8000/health\", timeout=3).read(); sys.exit(0)' || exit 1"],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ── ECR pull grants ──
    // ContainerImage.fromRegistry doesn't auto-grant pull (unlike
    // fromDockerImageAsset). Grant the task's execution role pull on
    // both: (1) the cdk-assets repo where the bootstrap image lives
    // (used on first deploy via the SSM-seeded URI), and (2) the
    // per-service ECR repo where the build pipeline pushes real
    // images.
    bootstrapImage.repository.grantPull(taskDefinition.obtainExecutionRole());
    ecrRepository.grantPull(taskDefinition.obtainExecutionRole());

    // ── IAM grants — refs pass-through replaces 30+ string props ──
    grantAppApiPermissions({
      scope: this,
      config,
      taskRole: taskDefinition.taskRole,
      refs: props.refs,
      agentCoreMemoryArn: props.agentCoreMemoryArn,
      sagemakerExecutionRoleArn: props.sagemakerExecutionRoleArn,
    });

    // ── Target group ──
    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'AppApiTargetGroup', {
      vpc,
      targetGroupName: getResourceName(config, 'app-api-tg'),
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/health',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        healthyHttpCodes: '200',
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // Route all traffic to this target group
    new elbv2.ApplicationListenerRule(this, 'AppApiListenerRule', {
      listener: albListener,
      priority: 1,
      conditions: [elbv2.ListenerCondition.pathPatterns(['/*'])],
      targetGroups: [targetGroup],
    });

    // Grant ECS task execution role pull rights on the project's
    // app-api ECR repo. CDK auto-grants pull on the cdk-assets repo
    // for the bootstrap image; we need this explicit grant for the
    // real image the backend workflow ships via
    // `aws ecs register-task-definition` + `update-service`.
    ecrRepository.grantPull(taskDefinition.executionRole!);

    // ── Fargate service ──
    this.ecsService = new ecs.FargateService(this, 'AppApiService', {
      cluster: ecsCluster,
      serviceName: getResourceName(config, 'app-api-service'),
      taskDefinition,
      desiredCount: config.appApi.desiredCount,
      securityGroups: [ecsSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      circuitBreaker: { enable: true, rollback: true },
      enableExecuteCommand: true,
    });

    this.ecsService.attachToApplicationTargetGroup(targetGroup);

    // ── Auto-scaling ──
    const scaling = this.ecsService.autoScaleTaskCount({
      minCapacity: config.appApi.desiredCount,
      maxCapacity: config.appApi.maxCapacity,
    });

    scaling.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    scaling.scaleOnMemoryUtilization('MemoryScaling', {
      targetUtilizationPercent: 80,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ── SSM publications ──
    // The backend workflow's deploy-app-api-code step needs:
    //   - the ECS cluster name to scope the service lookup
    //   - the ECS service name to call update-service against
    //   - the task definition family name to call
    //     register-task-definition (CDK-auto-generated; the
    //     workflow registers new revisions of the same family)
    new ssm.StringParameter(this, 'AppApiClusterNameParameter', {
      parameterName: `/${config.projectPrefix}/app-api/cluster-name`,
      stringValue: ecsCluster.clusterName,
      description: 'ECS cluster name for App API (consumed by the backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
    new ssm.StringParameter(this, 'AppApiServiceNameParameter', {
      parameterName: `/${config.projectPrefix}/app-api/service-name`,
      stringValue: this.ecsService.serviceName,
      description: 'ECS service name for App API (consumed by the backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
    new ssm.StringParameter(this, 'AppApiTaskDefFamilyParameter', {
      parameterName: `/${config.projectPrefix}/app-api/task-def-family`,
      stringValue: taskDefinition.family,
      description: 'ECS task definition family name for App API (consumed by the backend workflow code-deploy step to register new revisions)',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ── Outputs ──
    new cdk.CfnOutput(cdk.Stack.of(this), 'AppApiServiceName', {
      value: this.ecsService.serviceName,
      description: 'App API ECS Service Name',
    });

    new cdk.CfnOutput(cdk.Stack.of(this), 'AppApiTaskDefinitionArn', {
      value: taskDefinition.taskDefinitionArn,
      description: 'App API Task Definition ARN',
    });
  }
}
