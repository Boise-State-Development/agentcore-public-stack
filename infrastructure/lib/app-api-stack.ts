import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags } from './config';

export interface AppApiStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * App API Stack - Core Backend Infrastructure
 * 
 * This stack creates:
 * - VPC with public/private subnets across multiple AZs
 * - Application Load Balancer (ALB) in public subnets
 * - ECS Fargate cluster and service
 * - ECR repository for Docker images
 * - Database (DynamoDB or RDS Aurora Serverless v2)
 * - Security groups for network isolation
 * - SSM parameters for cross-stack references
 */
export class AppApiStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albListener: elbv2.ApplicationListener;
  public readonly ecsCluster: ecs.Cluster;
  public readonly ecsService: ecs.FargateService;

  constructor(scope: Construct, id: string, props: AppApiStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // VPC - Network Foundation
    // ============================================================
    this.vpc = new ec2.Vpc(this, 'AppApiVpc', {
      vpcName: getResourceName(config, 'app-api-vpc'),
      ipAddresses: ec2.IpAddresses.cidr(config.vpcCidr),
      maxAzs: 2, // Use 2 AZs for high availability
      natGateways: 1, // Single NAT Gateway for cost optimization (can be increased for HA)
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
      enableDnsHostnames: true,
      enableDnsSupport: true,
    });

    // Export VPC ID to SSM for cross-stack references
    new ssm.StringParameter(this, 'VpcIdParameter', {
      parameterName: `/${config.projectPrefix}/network/vpc-id`,
      stringValue: this.vpc.vpcId,
      description: 'VPC ID for App API Stack',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export Private Subnet IDs to SSM
    const privateSubnetIds = this.vpc.privateSubnets.map(subnet => subnet.subnetId).join(',');
    new ssm.StringParameter(this, 'PrivateSubnetIdsParameter', {
      parameterName: `/${config.projectPrefix}/network/private-subnet-ids`,
      stringValue: privateSubnetIds,
      description: 'Comma-separated list of private subnet IDs',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export Public Subnet IDs to SSM
    const publicSubnetIds = this.vpc.publicSubnets.map(subnet => subnet.subnetId).join(',');
    new ssm.StringParameter(this, 'PublicSubnetIdsParameter', {
      parameterName: `/${config.projectPrefix}/network/public-subnet-ids`,
      stringValue: publicSubnetIds,
      description: 'Comma-separated list of public subnet IDs',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Security Groups
    // ============================================================
    
    // ALB Security Group - Allow HTTP/HTTPS from internet
    const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: getResourceName(config, 'alb-sg'),
      description: 'Security group for Application Load Balancer',
      allowAllOutbound: true,
    });

    albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'Allow HTTP traffic from internet'
    );

    albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'Allow HTTPS traffic from internet'
    );

    // ECS Task Security Group - Allow traffic from ALB
    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: getResourceName(config, 'ecs-sg'),
      description: 'Security group for ECS Fargate tasks',
      allowAllOutbound: true,
    });

    ecsSecurityGroup.addIngressRule(
      albSecurityGroup,
      ec2.Port.tcp(8000),
      'Allow traffic from ALB to ECS tasks'
    );

    // ============================================================
    // Database Layer (Optional - controlled by config.appApi.databaseType)
    // ============================================================
    let databaseConnectionInfo: string | undefined;

    if (config.appApi.databaseType === 'none') {
      // No database configured - skip database creation
      // Set databaseType to 'dynamodb' or 'rds' in config when database is needed
    } else if (config.appApi.databaseType === 'dynamodb') {
      // DynamoDB Table
      const table = new dynamodb.Table(this, 'AppApiTable', {
        tableName: getResourceName(config, 'app-api-table'),
        partitionKey: {
          name: 'PK',
          type: dynamodb.AttributeType.STRING,
        },
        sortKey: {
          name: 'SK',
          type: dynamodb.AttributeType.STRING,
        },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        removalPolicy: cdk.RemovalPolicy.RETAIN, // Retain data on stack deletion
        pointInTimeRecovery: true, // TODO: Upgrade to pointInTimeRecoverySpecification when CDK version supports it
        encryption: dynamodb.TableEncryption.AWS_MANAGED,
      });

      // Add GSI for common query patterns
      table.addGlobalSecondaryIndex({
        indexName: 'GSI1',
        partitionKey: {
          name: 'GSI1PK',
          type: dynamodb.AttributeType.STRING,
        },
        sortKey: {
          name: 'GSI1SK',
          type: dynamodb.AttributeType.STRING,
        },
        projectionType: dynamodb.ProjectionType.ALL,
      });

      // Store table name in SSM
      new ssm.StringParameter(this, 'DynamoDbTableNameParameter', {
        parameterName: `/${config.projectPrefix}/database/table-name`,
        stringValue: table.tableName,
        description: 'DynamoDB table name for App API',
        tier: ssm.ParameterTier.STANDARD,
      });

      // Store table ARN in SSM
      new ssm.StringParameter(this, 'DynamoDbTableArnParameter', {
        parameterName: `/${config.projectPrefix}/database/table-arn`,
        stringValue: table.tableArn,
        description: 'DynamoDB table ARN for App API',
        tier: ssm.ParameterTier.STANDARD,
      });

      databaseConnectionInfo = table.tableName;

      // Output
      new cdk.CfnOutput(this, 'DynamoDbTableName', {
        value: table.tableName,
        description: 'DynamoDB table name',
        exportName: `${config.projectPrefix}-DynamoDbTableName`,
      });

    } else if (config.appApi.enableRds) {
      // RDS Aurora Serverless v2
      const dbSecurityGroup = new ec2.SecurityGroup(this, 'DatabaseSecurityGroup', {
        vpc: this.vpc,
        securityGroupName: getResourceName(config, 'db-sg'),
        description: 'Security group for RDS database',
        allowAllOutbound: false,
      });

      dbSecurityGroup.addIngressRule(
        ecsSecurityGroup,
        ec2.Port.tcp(5432), // PostgreSQL port (adjust for MySQL if needed)
        'Allow traffic from ECS tasks to RDS'
      );

      // Create database credentials in Secrets Manager
      const dbCredentials = new secretsmanager.Secret(this, 'DatabaseCredentials', {
        secretName: getResourceName(config, 'db-credentials'),
        description: 'Database credentials for App API RDS instance',
        generateSecretString: {
          secretStringTemplate: JSON.stringify({ username: 'appadmin' }),
          generateStringKey: 'password',
          excludePunctuation: true,
          includeSpace: false,
          passwordLength: 32,
        },
      });

      // RDS Aurora Serverless v2 Cluster
      const dbCluster = new rds.DatabaseCluster(this, 'DatabaseCluster', {
        clusterIdentifier: getResourceName(config, 'app-api-db'),
        engine: rds.DatabaseClusterEngine.auroraPostgres({
          version: rds.AuroraPostgresEngineVersion.VER_15_3,
        }),
        credentials: rds.Credentials.fromSecret(dbCredentials),
        defaultDatabaseName: config.appApi.rdsDatabaseName || 'appapi',
        vpc: this.vpc,
        vpcSubnets: {
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
        securityGroups: [dbSecurityGroup],
        serverlessV2MinCapacity: 0.5,
        serverlessV2MaxCapacity: 2,
        writer: rds.ClusterInstance.serverlessV2('writer'),
        readers: [
          rds.ClusterInstance.serverlessV2('reader', { scaleWithWriter: true }),
        ],
        backup: {
          retention: cdk.Duration.days(7),
          preferredWindow: '03:00-04:00',
        },
        cloudwatchLogsExports: ['postgresql'],
        removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
      });

      // Store database connection info in SSM (reference to secret)
      new ssm.StringParameter(this, 'DatabaseSecretArnParameter', {
        parameterName: `/${config.projectPrefix}/database/secret-arn`,
        stringValue: dbCredentials.secretArn,
        description: 'ARN of the Secrets Manager secret containing database credentials',
        tier: ssm.ParameterTier.STANDARD,
      });

      new ssm.StringParameter(this, 'DatabaseEndpointParameter', {
        parameterName: `/${config.projectPrefix}/database/endpoint`,
        stringValue: dbCluster.clusterEndpoint.hostname,
        description: 'RDS cluster endpoint hostname',
        tier: ssm.ParameterTier.STANDARD,
      });

      databaseConnectionInfo = dbCluster.clusterEndpoint.hostname;

      // Outputs
      new cdk.CfnOutput(this, 'DatabaseSecretArn', {
        value: dbCredentials.secretArn,
        description: 'ARN of database credentials secret',
        exportName: `${config.projectPrefix}-DatabaseSecretArn`,
      });

      new cdk.CfnOutput(this, 'DatabaseEndpoint', {
        value: dbCluster.clusterEndpoint.hostname,
        description: 'RDS cluster endpoint',
        exportName: `${config.projectPrefix}-DatabaseEndpoint`,
      });
    }

    // ============================================================
    // Application Load Balancer
    // ============================================================
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'AppApiLoadBalancer', {
      vpc: this.vpc,
      internetFacing: true,
      loadBalancerName: getResourceName(config, 'app-api-alb'),
      securityGroup: albSecurityGroup,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PUBLIC,
      },
    });

    // Store ALB ARN in SSM
    new ssm.StringParameter(this, 'AlbArnParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-arn`,
      stringValue: this.alb.loadBalancerArn,
      description: 'Application Load Balancer ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Store ALB DNS name in SSM
    new ssm.StringParameter(this, 'AlbDnsNameParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-dns-name`,
      stringValue: this.alb.loadBalancerDnsName,
      description: 'Application Load Balancer DNS name',
      tier: ssm.ParameterTier.STANDARD,
    });

    // HTTP Listener (port 80)
    this.albListener = this.alb.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultAction: elbv2.ListenerAction.fixedResponse(404, {
        contentType: 'text/plain',
        messageBody: 'Not Found',
      }),
    });

    // Store ALB Listener ARN in SSM
    new ssm.StringParameter(this, 'AlbListenerArnParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-listener-arn`,
      stringValue: this.albListener.listenerArn,
      description: 'Application Load Balancer HTTP Listener ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // ECS Cluster
    // ============================================================
    this.ecsCluster = new ecs.Cluster(this, 'AppApiCluster', {
      vpc: this.vpc,
      clusterName: getResourceName(config, 'app-api-cluster'),
      containerInsights: true, // TODO: Upgrade to containerInsightsV2 when CDK version supports it
    });

    // ============================================================
    // ECR Repository
    // ============================================================
    const ecrRepository = new ecr.Repository(this, 'AppApiRepository', {
      repositoryName: getResourceName(config, 'app-api'),
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          description: 'Keep last 10 images',
          maxImageCount: 10,
        },
      ],
    });

    // Store ECR repository URI in SSM
    new ssm.StringParameter(this, 'EcrRepositoryUriParameter', {
      parameterName: `/${config.projectPrefix}/app-api/ecr-repository-uri`,
      stringValue: ecrRepository.repositoryUri,
      description: 'ECR repository URI for App API',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // ECS Task Definition
    // ============================================================
    const taskDefinition = new ecs.FargateTaskDefinition(this, 'AppApiTaskDefinition', {
      family: getResourceName(config, 'app-api-task'),
      cpu: config.appApi.cpu,
      memoryLimitMiB: config.appApi.memory,
    });

    // Create log group for ECS task
    const logGroup = new logs.LogGroup(this, 'AppApiLogGroup', {
      logGroupName: `/ecs/${config.projectPrefix}/app-api`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Container Definition
    const container = taskDefinition.addContainer('AppApiContainer', {
      containerName: 'app-api',
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.11-slim'),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'app-api',
        logGroup: logGroup,
      }),
      environment: {
        AWS_REGION: config.awsRegion,
        PROJECT_PREFIX: config.projectPrefix,
        DATABASE_TYPE: config.appApi.databaseType,
        ...(databaseConnectionInfo && { DATABASE_CONNECTION: databaseConnectionInfo }),
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

    // Grant permissions for database access
    if (config.appApi.databaseType === 'dynamodb') {
      // Grant DynamoDB permissions (will be added after table is created)
      // This is a placeholder - actual permissions will be granted via IAM policy
    }

    // ============================================================
    // Target Group
    // ============================================================
    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'AppApiTargetGroup', {
      vpc: this.vpc,
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

    // Add listener rule for App API (root path)
    this.albListener.addTargetGroups('AppApiTargetGroupAttachment', {
      targetGroups: [targetGroup],
      priority: 1,
      conditions: [
        elbv2.ListenerCondition.pathPatterns(['/api/*', '/health']),
      ],
    });

    // ============================================================
    // ECS Fargate Service
    // ============================================================
    this.ecsService = new ecs.FargateService(this, 'AppApiService', {
      cluster: this.ecsCluster,
      serviceName: getResourceName(config, 'app-api-service'),
      taskDefinition: taskDefinition,
      desiredCount: config.appApi.desiredCount,
      securityGroups: [ecsSecurityGroup],
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      assignPublicIp: false,
      healthCheckGracePeriod: cdk.Duration.seconds(60),
      circuitBreaker: {
        rollback: true,
      },
    });

    // Attach service to target group
    this.ecsService.attachToApplicationTargetGroup(targetGroup);

    // Auto-scaling configuration
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

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: 'VPC ID',
      exportName: `${config.projectPrefix}-VpcId`,
    });

    new cdk.CfnOutput(this, 'AlbDnsName', {
      value: this.alb.loadBalancerDnsName,
      description: 'Application Load Balancer DNS Name',
      exportName: `${config.projectPrefix}-AlbDnsName`,
    });

    new cdk.CfnOutput(this, 'EcsClusterName', {
      value: this.ecsCluster.clusterName,
      description: 'ECS Cluster Name',
      exportName: `${config.projectPrefix}-EcsClusterName`,
    });

    new cdk.CfnOutput(this, 'EcsServiceName', {
      value: this.ecsService.serviceName,
      description: 'ECS Service Name',
      exportName: `${config.projectPrefix}-EcsServiceName`,
    });

    new cdk.CfnOutput(this, 'EcrRepositoryUri', {
      value: ecrRepository.repositoryUri,
      description: 'ECR Repository URI',
      exportName: `${config.projectPrefix}-AppApiEcrRepositoryUri`,
    });

    new cdk.CfnOutput(this, 'TaskDefinitionArn', {
      value: taskDefinition.taskDefinitionArn,
      description: 'Task Definition ARN',
      exportName: `${config.projectPrefix}-AppApiTaskDefinitionArn`,
    });
  }
}
