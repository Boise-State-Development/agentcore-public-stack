import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags, getRemovalPolicy, getAutoDeleteObjects } from './config';

export interface SageMakerFineTuningStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * SageMaker Fine-Tuning Stack - Optional ML Training Infrastructure
 *
 * This stack creates:
 * - DynamoDB tables for fine-tuning jobs and access control
 * - S3 bucket for fine-tuning datasets, model artifacts, and inference results
 * - IAM execution role for SageMaker training jobs
 * - Security group for SageMaker training jobs (outbound HTTPS only)
 *
 * Dependencies:
 * - VPC and network resources from Infrastructure Stack (imported via SSM)
 *
 * This stack is optional — controlled by config.fineTuning.enabled.
 * When disabled, AppApiStack functions normally without these resources.
 */
export class SageMakerFineTuningStack extends cdk.Stack {
  public readonly fineTuningJobsTable: dynamodb.Table;
  public readonly fineTuningAccessTable: dynamodb.Table;
  public readonly fineTuningDataBucket: s3.Bucket;
  public readonly sagemakerExecutionRole: iam.Role;
  public readonly sagemakerSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: SageMakerFineTuningStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // Import Network Resources from Infrastructure Stack
    // ============================================================

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

    // ============================================================
    // DynamoDB Tables
    // ============================================================

    // Fine-Tuning Jobs Table
    // PK: USER#{userId}, SK: JOB#{jobId}
    this.fineTuningJobsTable = new dynamodb.Table(this, 'FineTuningJobsTable', {
      tableName: getResourceName(config, 'fine-tuning-jobs'),
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
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI for querying jobs by status across all users (admin view)
    this.fineTuningJobsTable.addGlobalSecondaryIndex({
      indexName: 'StatusIndex',
      partitionKey: {
        name: 'status',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'createdAt',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // Fine-Tuning Access Table
    // PK: EMAIL#{email}, SK: ACCESS (fixed literal)
    this.fineTuningAccessTable = new dynamodb.Table(this, 'FineTuningAccessTable', {
      tableName: getResourceName(config, 'fine-tuning-access'),
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
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // ============================================================
    // S3 Bucket for Fine-Tuning Data
    // ============================================================

    // Build CORS origins for presigned URL uploads
    const corsOrigins = new Set<string>();
    corsOrigins.add('http://localhost:4200');
    if (config.domainName) {
      corsOrigins.add(`https://${config.domainName}`);
    }
    if (config.corsOrigins) {
      config.corsOrigins.split(',').map(o => o.trim()).filter(Boolean).forEach(o => corsOrigins.add(o));
    }
    const fineTuningCorsOrigins = Array.from(corsOrigins);

    this.fineTuningDataBucket = new s3.Bucket(this, 'FineTuningDataBucket', {
      bucketName: getResourceName(config, 'fine-tuning-data', config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
      cors: [
        {
          allowedOrigins: fineTuningCorsOrigins,
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.HEAD],
          allowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
          exposedHeaders: ['ETag', 'Content-Length', 'Content-Type'],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [
        {
          id: 'transition-to-ia',
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
        },
        {
          id: 'transition-to-glacier',
          transitions: [
            {
              storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: cdk.Duration.days(90),
            },
          ],
        },
        {
          id: 'expire-objects',
          expiration: cdk.Duration.days(365),
        },
        {
          id: 'abort-incomplete-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
    });

    // ============================================================
    // SageMaker Execution Role
    // ============================================================

    this.sagemakerExecutionRole = new iam.Role(this, 'SageMakerExecutionRole', {
      roleName: getResourceName(config, 'sagemaker-exec-role'),
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      description: 'Execution role assumed by SageMaker training and transform jobs',
    });

    // Grant S3 read/write on the fine-tuning data bucket
    this.fineTuningDataBucket.grantReadWrite(this.sagemakerExecutionRole);

    // Grant DynamoDB UpdateItem on jobs table (for progress writes from training scripts)
    this.sagemakerExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'FineTuningJobsProgressWrite',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:UpdateItem'],
        resources: [this.fineTuningJobsTable.tableArn],
      })
    );

    // Grant CloudWatch Logs permissions for training job logs
    this.sagemakerExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogsForTraining',
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
        ],
        resources: [
          `arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/sagemaker/*`,
        ],
      })
    );

    // ============================================================
    // SageMaker Security Group
    // ============================================================

    this.sagemakerSecurityGroup = new ec2.SecurityGroup(this, 'SageMakerSecurityGroup', {
      vpc: vpc,
      securityGroupName: getResourceName(config, 'sagemaker-sg'),
      description: 'Security group for SageMaker training jobs - outbound HTTPS only',
      allowAllOutbound: false,
    });

    // Allow outbound HTTPS only (S3, DynamoDB, CloudWatch, ECR, HuggingFace)
    this.sagemakerSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'Allow outbound HTTPS for AWS service access and model downloads'
    );

    // ============================================================
    // SSM Parameter Exports
    // ============================================================

    new ssm.StringParameter(this, 'JobsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/jobs-table-name`,
      stringValue: this.fineTuningJobsTable.tableName,
      description: 'Fine-tuning jobs DynamoDB table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'JobsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/jobs-table-arn`,
      stringValue: this.fineTuningJobsTable.tableArn,
      description: 'Fine-tuning jobs DynamoDB table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'AccessTableNameParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/access-table-name`,
      stringValue: this.fineTuningAccessTable.tableName,
      description: 'Fine-tuning access DynamoDB table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'AccessTableArnParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/access-table-arn`,
      stringValue: this.fineTuningAccessTable.tableArn,
      description: 'Fine-tuning access DynamoDB table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'DataBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/data-bucket-name`,
      stringValue: this.fineTuningDataBucket.bucketName,
      description: 'Fine-tuning data S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'DataBucketArnParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/data-bucket-arn`,
      stringValue: this.fineTuningDataBucket.bucketArn,
      description: 'Fine-tuning data S3 bucket ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SageMakerExecutionRoleArnParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/sagemaker-execution-role-arn`,
      stringValue: this.sagemakerExecutionRole.roleArn,
      description: 'SageMaker execution role ARN for training jobs',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SageMakerSecurityGroupIdParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/sagemaker-security-group-id`,
      stringValue: this.sagemakerSecurityGroup.securityGroupId,
      description: 'SageMaker training jobs security group ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'PrivateSubnetIdsParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/private-subnet-ids`,
      stringValue: privateSubnetIdsString,
      description: 'Private subnet IDs for SageMaker training jobs',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================

    new cdk.CfnOutput(this, 'FineTuningJobsTableName', {
      value: this.fineTuningJobsTable.tableName,
      description: 'Fine-tuning jobs DynamoDB table name',
      exportName: `${config.projectPrefix}-FineTuningJobsTableName`,
    });

    new cdk.CfnOutput(this, 'FineTuningAccessTableName', {
      value: this.fineTuningAccessTable.tableName,
      description: 'Fine-tuning access DynamoDB table name',
      exportName: `${config.projectPrefix}-FineTuningAccessTableName`,
    });

    new cdk.CfnOutput(this, 'FineTuningDataBucketName', {
      value: this.fineTuningDataBucket.bucketName,
      description: 'Fine-tuning data S3 bucket name',
      exportName: `${config.projectPrefix}-FineTuningDataBucketName`,
    });

    new cdk.CfnOutput(this, 'SageMakerExecutionRoleArn', {
      value: this.sagemakerExecutionRole.roleArn,
      description: 'SageMaker execution role ARN',
      exportName: `${config.projectPrefix}-SageMakerExecutionRoleArn`,
    });

    new cdk.CfnOutput(this, 'SageMakerSecurityGroupId', {
      value: this.sagemakerSecurityGroup.securityGroupId,
      description: 'SageMaker security group ID',
      exportName: `${config.projectPrefix}-SageMakerSecurityGroupId`,
    });
  }
}
