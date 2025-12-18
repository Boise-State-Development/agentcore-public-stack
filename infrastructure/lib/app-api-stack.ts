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
 * App API Stack - Core Backend Application
 * 
 * This stack creates:
 * - ECS Fargate service for App API
 * - Target group and listener rules for ALB routing
 * - Database (DynamoDB or RDS Aurora Serverless v2)
 * - Security groups for ECS tasks
 * 
 * Dependencies:
 * - VPC, ALB, ECS Cluster from Infrastructure Stack (imported via SSM)
 * 
 * Note: ECR repository is created by the build pipeline, not by CDK.
 */
export class AppApiStack extends cdk.Stack {
  public readonly ecsService: ecs.FargateService;

  constructor(scope: Construct, id: string, props: AppApiStackProps) {
    super(scope, id, props);

    const { config } = props;

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

    // Import image tag from SSM (set by push-to-ecr.sh)
    const imageTag = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/app-api/image-tag`
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
    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'AppEcsSecurityGroup', {
      vpc: vpc,
      securityGroupName: getResourceName(config, 'app-ecs-sg'),
      description: 'Security group for App API ECS Fargate tasks',
      allowAllOutbound: true,
    });

    ecsSecurityGroup.addIngressRule(
      albSecurityGroup,
      ec2.Port.tcp(8000),
      'Allow traffic from ALB to App API tasks'
    );

    // ============================================================
    // Quota Management Tables
    // ============================================================

    // UserQuotas Table
    const userQuotasTable = new dynamodb.Table(this, 'UserQuotasTable', {
      tableName: getResourceName(config, 'user-quotas'),
      partitionKey: {
        name: 'PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'SK',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: AssignmentTypeIndex - Query assignments by type, sorted by priority
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'AssignmentTypeIndex',
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

    // GSI2: UserAssignmentIndex - Query direct user assignments (O(1) lookup)
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserAssignmentIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI3: RoleAssignmentIndex - Query role-based assignments, sorted by priority
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'RoleAssignmentIndex',
      partitionKey: {
        name: 'GSI3PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // QuotaEvents Table
    const quotaEventsTable = new dynamodb.Table(this, 'QuotaEventsTable', {
      tableName: getResourceName(config, 'quota-events'),
      partitionKey: {
        name: 'PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'SK',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI5: TierEventIndex - Query events by tier for analytics
    quotaEventsTable.addGlobalSecondaryIndex({
      indexName: 'TierEventIndex',
      partitionKey: {
        name: 'GSI5PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI5SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store quota table names in SSM
    new ssm.StringParameter(this, 'UserQuotasTableNameParameter', {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-name`,
      stringValue: userQuotasTable.tableName,
      description: 'UserQuotas table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserQuotasTableArnParameter', {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-arn`,
      stringValue: userQuotasTable.tableArn,
      description: 'UserQuotas table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'QuotaEventsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-name`,
      stringValue: quotaEventsTable.tableName,
      description: 'QuotaEvents table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'QuotaEventsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-arn`,
      stringValue: quotaEventsTable.tableArn,
      description: 'QuotaEvents table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });


    // // ============================================================
    // // Database Layer (Optional - controlled by config.appApi.databaseType)
    // // ============================================================
    // let databaseConnectionInfo: string | undefined;

    // if (config.appApi.databaseType === 'none') {
    //   // No database configured - skip database creation
    //   // Set databaseType to 'dynamodb' or 'rds' in config when database is needed
    // } else if (config.appApi.databaseType === 'dynamodb') {
    //   // DynamoDB Table
    //   const table = new dynamodb.Table(this, 'AppApiTable', {
    //     tableName: getResourceName(config, 'app-api-table'),
    //     partitionKey: {
    //       name: 'PK',
    //       type: dynamodb.AttributeType.STRING,
    //     },
    //     sortKey: {
    //       name: 'SK',
    //       type: dynamodb.AttributeType.STRING,
    //     },
    //     billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
    //     removalPolicy: cdk.RemovalPolicy.RETAIN, // Retain data on stack deletion
    //     pointInTimeRecovery: true, // TODO: Upgrade to pointInTimeRecoverySpecification when CDK version supports it
    //     encryption: dynamodb.TableEncryption.AWS_MANAGED,
    //   });

    //   // Add GSI for common query patterns
    //   table.addGlobalSecondaryIndex({
    //     indexName: 'GSI1',
    //     partitionKey: {
    //       name: 'GSI1PK',
    //       type: dynamodb.AttributeType.STRING,
    //     },
    //     sortKey: {
    //       name: 'GSI1SK',
    //       type: dynamodb.AttributeType.STRING,
    //     },
    //     projectionType: dynamodb.ProjectionType.ALL,
    //   });

    //   // Store table name in SSM
    //   new ssm.StringParameter(this, 'DynamoDbTableNameParameter', {
    //     parameterName: `/${config.projectPrefix}/database/table-name`,
    //     stringValue: table.tableName,
    //     description: 'DynamoDB table name for App API',
    //     tier: ssm.ParameterTier.STANDARD,
    //   });

    //   // Store table ARN in SSM
    //   new ssm.StringParameter(this, 'DynamoDbTableArnParameter', {
    //     parameterName: `/${config.projectPrefix}/database/table-arn`,
    //     stringValue: table.tableArn,
    //     description: 'DynamoDB table ARN for App API',
    //     tier: ssm.ParameterTier.STANDARD,
    //   });

    //   databaseConnectionInfo = table.tableName;

    //   // Output
    //   new cdk.CfnOutput(this, 'DynamoDbTableName', {
    //     value: table.tableName,
    //     description: 'DynamoDB table name',
    //     exportName: `${config.projectPrefix}-DynamoDbTableName`,
    //   });

    // } else if (config.appApi.enableRds) {
    //   // RDS Aurora Serverless v2
    //   const dbSecurityGroup = new ec2.SecurityGroup(this, 'DatabaseSecurityGroup', {
    //     vpc: this.vpc,
    //     securityGroupName: getResourceName(config, 'db-sg'),
    //     description: 'Security group for RDS database',
    //     allowAllOutbound: false,
    //   });

    //   dbSecurityGroup.addIngressRule(
    //     ecsSecurityGroup,
    //     ec2.Port.tcp(5432), // PostgreSQL port (adjust for MySQL if needed)
    //     'Allow traffic from ECS tasks to RDS'
    //   );

    //   // Create database credentials in Secrets Manager
    //   const dbCredentials = new secretsmanager.Secret(this, 'DatabaseCredentials', {
    //     secretName: getResourceName(config, 'db-credentials'),
    //     description: 'Database credentials for App API RDS instance',
    //     generateSecretString: {
    //       secretStringTemplate: JSON.stringify({ username: 'appadmin' }),
    //       generateStringKey: 'password',
    //       excludePunctuation: true,
    //       includeSpace: false,
    //       passwordLength: 32,
    //     },
    //   });

    //   // RDS Aurora Serverless v2 Cluster
    //   const dbCluster = new rds.DatabaseCluster(this, 'DatabaseCluster', {
    //     clusterIdentifier: getResourceName(config, 'app-api-db'),
    //     engine: rds.DatabaseClusterEngine.auroraPostgres({
    //       version: rds.AuroraPostgresEngineVersion.VER_15_3,
    //     }),
    //     credentials: rds.Credentials.fromSecret(dbCredentials),
    //     defaultDatabaseName: config.appApi.rdsDatabaseName || 'appapi',
    //     vpc: this.vpc,
    //     vpcSubnets: {
    //       subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    //     },
    //     securityGroups: [dbSecurityGroup],
    //     serverlessV2MinCapacity: 0.5,
    //     serverlessV2MaxCapacity: 2,
    //     writer: rds.ClusterInstance.serverlessV2('writer'),
    //     readers: [
    //       rds.ClusterInstance.serverlessV2('reader', { scaleWithWriter: true }),
    //     ],
    //     backup: {
    //       retention: cdk.Duration.days(7),
    //       preferredWindow: '03:00-04:00',
    //     },
    //     cloudwatchLogsExports: ['postgresql'],
    //     removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
    //   });

    //   // Store database connection info in SSM (reference to secret)
    //   new ssm.StringParameter(this, 'DatabaseSecretArnParameter', {
    //     parameterName: `/${config.projectPrefix}/database/secret-arn`,
    //     stringValue: dbCredentials.secretArn,
    //     description: 'ARN of the Secrets Manager secret containing database credentials',
    //     tier: ssm.ParameterTier.STANDARD,
    //   });

    //   new ssm.StringParameter(this, 'DatabaseEndpointParameter', {
    //     parameterName: `/${config.projectPrefix}/database/endpoint`,
    //     stringValue: dbCluster.clusterEndpoint.hostname,
    //     description: 'RDS cluster endpoint hostname',
    //     tier: ssm.ParameterTier.STANDARD,
    //   });

    //   databaseConnectionInfo = dbCluster.clusterEndpoint.hostname;

    //   // Outputs
    //   new cdk.CfnOutput(this, 'DatabaseSecretArn', {
    //     value: dbCredentials.secretArn,
    //     description: 'ARN of database credentials secret',
    //     exportName: `${config.projectPrefix}-DatabaseSecretArn`,
    //   });

    //   new cdk.CfnOutput(this, 'DatabaseEndpoint', {
    //     value: dbCluster.clusterEndpoint.hostname,
    //     description: 'RDS cluster endpoint',
    //     exportName: `${config.projectPrefix}-DatabaseEndpoint`,
    //   });
    // }

    // ============================================================
    // ECS Task Definition
    // ============================================================
    // Note: ECR Repository is created automatically by the build pipeline
    // when pushing the first Docker image (see scripts/stack-app-api/push-to-ecr.sh)
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

    // Reference the ECR repository created by the build pipeline
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this,
      'AppApiRepository',
      getResourceName(config, 'app-api')
    );

    // Container Definition
    const container = taskDefinition.addContainer('AppApiContainer', {
      containerName: 'app-api',
      image: ecs.ContainerImage.fromEcrRepository(ecrRepository, imageTag),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'app-api',
        logGroup: logGroup,
      }),
      environment: {
        AWS_REGION: config.awsRegion,
        PROJECT_PREFIX: config.projectPrefix,
        // DATABASE_TYPE: config.appApi.databaseType,
        // ...(databaseConnectionInfo && { DATABASE_CONNECTION: databaseConnectionInfo }),
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

    // Grant permissions for quota management tables
    userQuotasTable.grantReadWriteData(taskDefinition.taskRole);
    quotaEventsTable.grantReadWriteData(taskDefinition.taskRole);

    // ============================================================
    // Target Group
    // ============================================================
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

    // Add listener rule for App API (root path)
    albListener.addTargetGroups('AppApiTargetGroupAttachment', {
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
      cluster: ecsCluster,
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
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
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
    new cdk.CfnOutput(this, 'EcsServiceName', {
      value: this.ecsService.serviceName,
      description: 'ECS Service Name',
      exportName: `${config.projectPrefix}-AppEcsServiceName`,
    });

    new cdk.CfnOutput(this, 'TaskDefinitionArn', {
      value: taskDefinition.taskDefinitionArn,
      description: 'Task Definition ARN',
      exportName: `${config.projectPrefix}-AppApiTaskDefinitionArn`,
    });

    new cdk.CfnOutput(this, 'UserQuotasTableName', {
      value: userQuotasTable.tableName,
      description: 'UserQuotas table name',
      exportName: `${config.projectPrefix}-UserQuotasTableName`,
    });

    new cdk.CfnOutput(this, 'QuotaEventsTableName', {
      value: quotaEventsTable.tableName,
      description: 'QuotaEvents table name',
      exportName: `${config.projectPrefix}-QuotaEventsTableName`,
    });
  }
}
