# ECS/Fargate Patterns

## Task Definition

```typescript
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as logs from 'aws-cdk-lib/aws-logs';
import { getResourceName } from './config';

const taskDefinition = new ecs.FargateTaskDefinition(this, 'AppApiTaskDef', {
  family: getResourceName(config, 'app-api-task'),
  cpu: config.appApi.cpu,           // 512, 1024, 2048, 4096
  memoryLimitMiB: config.appApi.memory,  // Must match CPU tier
});

// Log group per service
const logGroup = new logs.LogGroup(this, 'AppApiLogGroup', {
  logGroupName: `/ecs/${config.projectPrefix}/app-api`,
  retention: logs.RetentionDays.ONE_WEEK,
  removalPolicy: cdk.RemovalPolicy.DESTROY,
});
```

## Container Configuration

```typescript
const container = taskDefinition.addContainer('AppApiContainer', {
  containerName: 'app-api',
  image: ecs.ContainerImage.fromEcrRepository(ecrRepository, imageTag),
  logging: ecs.LogDrivers.awsLogs({
    streamPrefix: 'app-api',
    logGroup,
  }),

  // Environment variables for all resources
  environment: {
    PROJECT_PREFIX: config.projectPrefix,
    AWS_REGION: config.awsRegion,
    // DynamoDB table names
    DYNAMODB_USER_QUOTAS_TABLE: userQuotasTable.tableName,
    DYNAMODB_QUOTA_EVENTS_TABLE: quotaEventsTable.tableName,
    DYNAMODB_SESSIONS_METADATA_TABLE: sessionsMetadataTable.tableName,
    // S3 bucket names
    S3_USER_FILES_BUCKET: userFilesBucket.bucketName,
    // Service URLs
    INFERENCE_API_URL: `http://${inferenceApiHostname}:8001`,
  },

  // Health check (mandatory)
  healthCheck: {
    command: ['CMD-SHELL', 'curl -f http://localhost:8000/health || exit 1'],
    interval: cdk.Duration.seconds(30),
    timeout: cdk.Duration.seconds(5),
    retries: 3,
    startPeriod: cdk.Duration.seconds(60),
  },

  // Port mapping
  portMappings: [{
    containerPort: 8000,
    protocol: ecs.Protocol.TCP,
  }],
});
```

## Fargate Service

```typescript
import * as ec2 from 'aws-cdk-lib/aws-ec2';

const service = new ecs.FargateService(this, 'AppApiService', {
  cluster: ecsCluster,
  serviceName: getResourceName(config, 'app-api-service'),
  taskDefinition: taskDefinition,
  desiredCount: config.appApi.desiredCount,

  // Security - private subnets, no public IP
  securityGroups: [ecsSecurityGroup],
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  assignPublicIp: false,

  // Health & rolling deployment
  healthCheckGracePeriod: cdk.Duration.seconds(60),
  circuitBreaker: { rollback: true },  // Auto-rollback on failure
  minHealthyPercent: 100,              // Always have healthy task
  maxHealthyPercent: 200,              // Allow 2x for rolling update

  // Enable ECS Exec for debugging (optional)
  enableExecuteCommand: config.environment !== 'prod',
});

// Attach to ALB target group
service.attachToApplicationTargetGroup(targetGroup);
```

## Auto-Scaling

```typescript
const scaling = service.autoScaleTaskCount({
  minCapacity: config.appApi.desiredCount,
  maxCapacity: config.appApi.maxCapacity,
});

// CPU-based scaling
scaling.scaleOnCpuUtilization('CpuScaling', {
  targetUtilizationPercent: 70,
  scaleInCooldown: cdk.Duration.seconds(60),
  scaleOutCooldown: cdk.Duration.seconds(60),
});

// Memory-based scaling
scaling.scaleOnMemoryUtilization('MemoryScaling', {
  targetUtilizationPercent: 80,
  scaleInCooldown: cdk.Duration.seconds(60),
  scaleOutCooldown: cdk.Duration.seconds(60),
});

// Optional: Request count scaling
scaling.scaleOnRequestCount('RequestScaling', {
  targetGroup: targetGroup,
  requestsPerTarget: 1000,
  scaleInCooldown: cdk.Duration.seconds(60),
  scaleOutCooldown: cdk.Duration.seconds(60),
});
```

## Target Group

```typescript
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';

const targetGroup = new elbv2.ApplicationTargetGroup(this, 'AppApiTargetGroup', {
  vpc: vpc,
  targetGroupName: getResourceName(config, 'app-api-tg'),
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

// Add listener rule with path pattern
albListener.addTargetGroups('AppApiTarget', {
  targetGroups: [targetGroup],
  priority: 1,  // Lower number = higher priority
  conditions: [
    elbv2.ListenerCondition.pathPatterns(['/api/*']),
  ],
});
```

## Importing Network Resources

```typescript
// Import VPC
const vpcId = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/vpc-id`
);

const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
  vpcId: vpcId,
  availabilityZones: cdk.Fn.split(',', ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/network/availability-zones`
  )),
  privateSubnetIds: cdk.Fn.split(',', ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/network/private-subnet-ids`
  )),
});

// Import ECS Cluster
const clusterName = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/ecs-cluster-name`
);

const ecsCluster = ecs.Cluster.fromClusterAttributes(this, 'ImportedCluster', {
  clusterName: clusterName,
  vpc: vpc,
  securityGroups: [],
});

// Import ALB Listener
const listenerArn = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/alb-listener-arn`
);

const albListener = elbv2.ApplicationListener.fromApplicationListenerAttributes(
  this,
  'ImportedListener',
  {
    listenerArn: listenerArn,
    securityGroup: ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'ImportedAlbSg',
      ssm.StringParameter.valueForStringParameter(
        this,
        `/${config.projectPrefix}/network/alb-security-group-id`
      )
    ),
  }
);
```

## Security Group for ECS Tasks

```typescript
const ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
  vpc: vpc,
  securityGroupName: getResourceName(config, 'app-api-ecs-sg'),
  description: 'Security group for App API ECS tasks',
  allowAllOutbound: true,
});

// Allow traffic from ALB only
const albSecurityGroupId = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/alb-security-group-id`
);

ecsSecurityGroup.addIngressRule(
  ec2.SecurityGroup.fromSecurityGroupId(this, 'AlbSg', albSecurityGroupId),
  ec2.Port.tcp(8000),
  'Allow traffic from ALB'
);
```

## Granting Database Access

```typescript
// Grant DynamoDB permissions
userQuotasTable.grantReadWriteData(taskDefinition.taskRole);
quotaEventsTable.grantReadWriteData(taskDefinition.taskRole);
sessionsMetadataTable.grantReadWriteData(taskDefinition.taskRole);

// Grant S3 permissions
userFilesBucket.grantReadWrite(taskDefinition.taskRole);

// Grant Secrets Manager access
secret.grantRead(taskDefinition.taskRole);
```

## ECR Repository Import

```typescript
// ECR repository created by CI/CD pipeline
const ecrRepository = ecr.Repository.fromRepositoryName(
  this,
  'AppApiRepository',
  `${config.projectPrefix}/app-api`
);

// Use latest or specific tag
const imageTag = config.appApi.imageTag || 'latest';
```
