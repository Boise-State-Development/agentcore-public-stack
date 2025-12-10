import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags } from './config';

export interface InferenceApiStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Inference API Stack - AI Workload Infrastructure
 * 
 * This stack creates:
 * - ECS Fargate service for inference workloads
 * - Target group for ALB integration
 * - Listener rule for routing /inference/* paths
 * - Higher CPU/memory allocation for AI workloads
 * 
 * Dependencies:
 * - VPC, ALB, Listener, ECS Cluster from Infrastructure Stack (imported via SSM)
 * 
 * Note: ECR repository is created by the build pipeline, not by CDK.
 */
export class InferenceApiStack extends cdk.Stack {
  public readonly ecsService: ecs.FargateService;

  constructor(scope: Construct, id: string, props: InferenceApiStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Validate imageTag is provided
    if (!config.inferenceApi.imageTag) {
      throw new Error(
        'InferenceApiStack requires imageTag to be provided. Pass --context imageTag=<tag> when deploying.'
      );
    }

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // Import Network Resources from Infrastructure Stack
    // ============================================================
    
    // Import VPC
    const vpcId = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/vpc-id`
    );
    const vpcCidr = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/vpc-cidr`
    );
    const privateSubnetIdsString = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/private-subnet-ids`
    );
    const availabilityZonesString = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/availability-zones`
    );

    const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
      vpcId: vpcId,
      vpcCidrBlock: vpcCidr,
      availabilityZones: cdk.Fn.split(',', availabilityZonesString),
      privateSubnetIds: cdk.Fn.split(',', privateSubnetIdsString),
    });

    // Import ALB Security Group
    const albSecurityGroupId = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/alb-security-group-id`
    );
    const albSecurityGroup = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'ImportedAlbSecurityGroup',
      albSecurityGroupId
    );

    // Import ALB
    const albArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/alb-arn`
    );
    const alb = elbv2.ApplicationLoadBalancer.fromApplicationLoadBalancerAttributes(
      this,
      'ImportedAlb',
      {
        loadBalancerArn: albArn,
        securityGroupId: albSecurityGroupId,
      }
    );

    // Import ALB Listener
    const albListenerArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/alb-listener-arn`
    );
    const albListener = elbv2.ApplicationListener.fromApplicationListenerAttributes(
      this,
      'ImportedAlbListener',
      {
        listenerArn: albListenerArn,
        securityGroup: albSecurityGroup,
      }
    );

    // Import ECS Cluster
    const ecsClusterName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/ecs-cluster-name`
    );
    const ecsClusterArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/ecs-cluster-arn`
    );
    const ecsCluster = ecs.Cluster.fromClusterAttributes(this, 'ImportedEcsCluster', {
      clusterName: ecsClusterName,
      clusterArn: ecsClusterArn,
      vpc: vpc,
      securityGroups: [],
    });

    // ============================================================
    // Security Groups
    // ============================================================
    
    // ECS Task Security Group - Allow traffic from ALB
    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'InferenceEcsSecurityGroup', {
      vpc: vpc,
      securityGroupName: getResourceName(config, 'inference-ecs-sg'),
      description: 'Security group for Inference API ECS Fargate tasks',
      allowAllOutbound: true,
    });

    // Allow inbound traffic from ALB on port 8000
    ecsSecurityGroup.addIngressRule(
      albSecurityGroup,
      ec2.Port.tcp(8000),
      'Allow traffic from ALB to Inference API tasks'
    );

    // ============================================================
    // ECS Task Definition
    // ============================================================
    
    // Note: ECR Repository is created automatically by the build pipeline
    // when pushing the first Docker image (see scripts/stack-inference-api/push-to-ecr.sh)
    const taskDefinition = new ecs.FargateTaskDefinition(this, 'InferenceApiTaskDefinition', {
      family: getResourceName(config, 'inference-api-task'),
      cpu: config.inferenceApi.cpu, // Higher CPU for inference workloads
      memoryLimitMiB: config.inferenceApi.memory, // Higher memory for inference workloads
    });

    // Create log group for ECS task
    const logGroup = new logs.LogGroup(this, 'InferenceApiLogGroup', {
      logGroupName: `/ecs/${config.projectPrefix}/inference-api`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Reference the ECR repository created by the build pipeline
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this,
      'InferenceApiRepository',
      getResourceName(config, 'inference-api')
    );

    // Container Definition
    const container = taskDefinition.addContainer('InferenceApiContainer', {
      containerName: 'inference-api',
      image: ecs.ContainerImage.fromEcrRepository(ecrRepository, config.inferenceApi.imageTag),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'inference-api',
        logGroup: logGroup,
      }),
      environment: {
        AWS_REGION: config.awsRegion,
        PROJECT_PREFIX: config.projectPrefix,
        // Add any inference-specific environment variables here
      },
      portMappings: [
        {
          containerPort: 8000,
          protocol: ecs.Protocol.TCP,
        },
      ],
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8000/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ============================================================
    // Target Group
    // ============================================================
    
    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'InferenceApiTargetGroup', {
      vpc: vpc,
      targetGroupName: getResourceName(config, 'inference-api-tg'),
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        enabled: true,
        path: '/health',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        healthyHttpCodes: '200',
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // ============================================================
    // Listener Rule - Route /inference/** to Inference API
    // ============================================================
    
    new elbv2.ApplicationListenerRule(this, 'InferenceApiListenerRule', {
      listener: albListener,
      priority: 10, // Higher priority than App API (which uses priority 1)
      conditions: [
        elbv2.ListenerCondition.pathPatterns(['/inference/*', '/inference']),
      ],
      action: elbv2.ListenerAction.forward([targetGroup]),
    });

    // ============================================================
    // ECS Fargate Service
    // ============================================================
    
    this.ecsService = new ecs.FargateService(this, 'InferenceApiService', {
      cluster: ecsCluster,
      serviceName: getResourceName(config, 'inference-api-service'),
      taskDefinition: taskDefinition,
      desiredCount: config.inferenceApi.desiredCount,
      securityGroups: [ecsSecurityGroup],
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      assignPublicIp: false,
      healthCheckGracePeriod: cdk.Duration.seconds(60),
      circuitBreaker: {
        rollback: true,
      },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    // Attach service to target group
    this.ecsService.attachToApplicationTargetGroup(targetGroup);

    // Auto-scaling configuration
    const scaling = this.ecsService.autoScaleTaskCount({
      minCapacity: config.inferenceApi.desiredCount,
      maxCapacity: config.inferenceApi.maxCapacity,
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

    // ============================================================
    // SSM Parameters for Cross-Stack References
    // ============================================================
    
    new ssm.StringParameter(this, 'InferenceApiServiceNameParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/service-name`,
      stringValue: this.ecsService.serviceName,
      description: 'Inference API ECS Service Name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiTaskDefinitionArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/task-definition-arn`,
      stringValue: taskDefinition.taskDefinitionArn,
      description: 'Inference API Task Definition ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    
    new cdk.CfnOutput(this, 'InferenceApiServiceName', {
      value: this.ecsService.serviceName,
      description: 'Inference API ECS Service Name',
      exportName: `${config.projectPrefix}-InferenceApiServiceName`,
    });

    new cdk.CfnOutput(this, 'InferenceApiTaskDefinitionArn', {
      value: taskDefinition.taskDefinitionArn,
      description: 'Inference API Task Definition ARN',
      exportName: `${config.projectPrefix}-InferenceApiTaskDefinitionArn`,
    });

    new cdk.CfnOutput(this, 'InferenceApiTargetGroupArn', {
      value: targetGroup.targetGroupArn,
      description: 'Inference API Target Group ARN',
      exportName: `${config.projectPrefix}-InferenceApiTargetGroupArn`,
    });
  }
}
