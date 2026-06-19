import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface SageMakerExecutionRoleConstructProps {
  config: AppConfig;
  /** Fine-tuning data bucket — granted read/write by the role. */
  dataBucket: s3.IBucket;
  /** Fine-tuning jobs table — granted UpdateItem for progress writes. */
  jobsTable: dynamodb.ITable;
  /** VPC the SageMaker security group is created in. */
  vpc: ec2.IVpc;
  /** Comma-separated private subnet ID string for the SSM publication. */
  privateSubnetIdsString: string;
}

/**
 * SageMakerExecutionRoleConstruct — IAM execution role + security
 * group for SageMaker training jobs.
 *
 * The role is assumed by `sagemaker.amazonaws.com` and granted:
 *   - Read/write on the supplied fine-tuning data bucket
 *   - UpdateItem on the supplied jobs table (for in-job progress writes)
 *   - VPC networking actions required for VPC-based training jobs
 *   - CloudWatch Logs publish under /aws/sagemaker/*
 *
 * The security group permits outbound HTTPS only (S3, DynamoDB,
 * CloudWatch, ECR, HuggingFace), no inbound — training jobs initiate
 * all their connections.
 *
 * SSM publications:
 *   /{prefix}/fine-tuning/sagemaker-execution-role-arn
 *   /{prefix}/fine-tuning/sagemaker-security-group-id
 *   /{prefix}/fine-tuning/private-subnet-ids
 */
export class SageMakerExecutionRoleConstruct extends Construct {
  public readonly executionRole: iam.Role;
  public readonly securityGroup: ec2.SecurityGroup;

  constructor(
    scope: Construct,
    id: string,
    props: SageMakerExecutionRoleConstructProps,
  ) {
    super(scope, id);

    const { config, dataBucket, jobsTable, vpc, privateSubnetIdsString } =
      props;

    // Keep an explicit, stable roleName for consistency with the other
    // execution roles and to avoid replacing the role (and the dependent
    // app-api PassRole grant) on already-deployed stacks. Orphaned-role
    // collisions on a fresh deploy are handled by deleting the orphans.
    this.executionRole = new iam.Role(this, 'SageMakerExecutionRole', {
      roleName: getResourceName(config, 'sagemaker-exec-role'),
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      description:
        'Execution role assumed by SageMaker training and transform jobs',
    });

    dataBucket.grantReadWrite(this.executionRole);

    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'FineTuningJobsProgressWrite',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:UpdateItem'],
        resources: [jobsTable.tableArn],
      }),
    );

    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'VpcNetworkingForTraining',
        effect: iam.Effect.ALLOW,
        actions: [
          'ec2:DescribeSubnets',
          'ec2:DescribeSecurityGroups',
          'ec2:DescribeNetworkInterfaces',
          'ec2:DescribeVpcs',
          'ec2:DescribeDhcpOptions',
          'ec2:CreateNetworkInterface',
          'ec2:CreateNetworkInterfacePermission',
          'ec2:DeleteNetworkInterface',
        ],
        resources: ['*'],
      }),
    );

    this.executionRole.addToPolicy(
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
      }),
    );

    this.securityGroup = new ec2.SecurityGroup(
      this,
      'SageMakerSecurityGroup',
      {
        vpc,
        securityGroupName: getResourceName(config, 'sagemaker-sg'),
        description:
          'Security group for SageMaker training jobs - outbound HTTPS only',
        allowAllOutbound: false,
      },
    );

    this.securityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'Allow outbound HTTPS for AWS service access and model downloads',
    );



  }
}
