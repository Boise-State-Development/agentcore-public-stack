import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { SageMakerFineTuningStack } from '../lib/sagemaker-fine-tuning-stack';
import { createMockConfig, mockSsmContext, mockEnv } from './helpers/mock-config';

/**
 * Unit Tests for SageMaker Fine-Tuning Stack
 *
 * These tests verify that the SageMakerFineTuningStack creates all required
 * resources with correct configurations: DynamoDB tables, S3 bucket, IAM
 * execution role, security group, and SSM parameter exports.
 */

describe('SageMakerFineTuningStack', () => {
  let app: cdk.App;
  let stack: SageMakerFineTuningStack;
  let template: Template;

  beforeEach(() => {
    const config = createMockConfig({ fineTuning: { enabled: true } });
    app = new cdk.App();
    mockSsmContext(app, config, ['SageMakerFineTuningStack']);

    stack = new SageMakerFineTuningStack(app, 'TestSageMakerFineTuningStack', {
      config,
      env: mockEnv(config),
    });

    template = Template.fromStack(stack);
  });

  // ============================================================
  // DynamoDB Tables
  // ============================================================

  describe('Fine-Tuning Jobs Table', () => {
    test('creates table with correct name', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-jobs',
      });
    });

    test('configures PK and SK keys', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-jobs',
        KeySchema: [
          { AttributeName: 'PK', KeyType: 'HASH' },
          { AttributeName: 'SK', KeyType: 'RANGE' },
        ],
        AttributeDefinitions: Match.arrayWith([
          { AttributeName: 'PK', AttributeType: 'S' },
          { AttributeName: 'SK', AttributeType: 'S' },
        ]),
      });
    });

    test('uses PAY_PER_REQUEST billing mode', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-jobs',
        BillingMode: 'PAY_PER_REQUEST',
      });
    });

    test('enables point-in-time recovery', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-jobs',
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
        },
      });
    });

    test('uses AWS_MANAGED encryption', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-jobs',
        SSESpecification: {
          SSEEnabled: true,
        },
      });
    });

    test('creates StatusIndex GSI', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-jobs',
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: 'StatusIndex',
            KeySchema: [
              { AttributeName: 'status', KeyType: 'HASH' },
              { AttributeName: 'createdAt', KeyType: 'RANGE' },
            ],
          }),
        ]),
      });
    });
  });

  describe('Fine-Tuning Access Table', () => {
    test('creates table with correct name', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-access',
      });
    });

    test('configures PK and SK keys', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-access',
        KeySchema: [
          { AttributeName: 'PK', KeyType: 'HASH' },
          { AttributeName: 'SK', KeyType: 'RANGE' },
        ],
      });
    });

    test('uses PAY_PER_REQUEST billing mode', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-access',
        BillingMode: 'PAY_PER_REQUEST',
      });
    });

    test('enables point-in-time recovery', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-fine-tuning-access',
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
        },
      });
    });
  });

  // ============================================================
  // S3 Bucket
  // ============================================================

  describe('Fine-Tuning Data Bucket', () => {
    test('creates bucket with correct name', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: 'test-project-fine-tuning-data-123456789012',
      });
    });

    test('configures S3_MANAGED encryption', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            {
              ServerSideEncryptionByDefault: {
                SSEAlgorithm: 'AES256',
              },
            },
          ],
        },
      });
    });

    test('blocks all public access', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
    });

    test('configures CORS for presigned uploads', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        CorsConfiguration: {
          CorsRules: [
            {
              AllowedOrigins: Match.arrayWith(['http://localhost:4200']),
              AllowedMethods: ['GET', 'PUT', 'HEAD'],
              AllowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
              MaxAge: 3600,
            },
          ],
        },
      });
    });

    test('configures lifecycle rules', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        LifecycleConfiguration: {
          Rules: Match.arrayWith([
            Match.objectLike({
              Id: 'transition-to-ia',
              Transitions: [
                { StorageClass: 'STANDARD_IA', TransitionInDays: 30 },
              ],
              Status: 'Enabled',
            }),
            Match.objectLike({
              Id: 'transition-to-glacier',
              Transitions: [
                { StorageClass: 'GLACIER_IR', TransitionInDays: 90 },
              ],
              Status: 'Enabled',
            }),
            Match.objectLike({
              Id: 'expire-objects',
              ExpirationInDays: 365,
              Status: 'Enabled',
            }),
            Match.objectLike({
              Id: 'abort-incomplete-multipart',
              AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
              Status: 'Enabled',
            }),
          ]),
        },
      });
    });
  });

  // ============================================================
  // IAM Role
  // ============================================================

  describe('SageMaker Execution Role', () => {
    test('creates role with correct name and trust policy', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        RoleName: 'test-project-sagemaker-exec-role',
        AssumeRolePolicyDocument: {
          Statement: [
            {
              Action: 'sts:AssumeRole',
              Effect: 'Allow',
              Principal: {
                Service: 'sagemaker.amazonaws.com',
              },
            },
          ],
        },
      });
    });

    test('grants DynamoDB UpdateItem on jobs table', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'FineTuningJobsProgressWrite',
              Action: 'dynamodb:UpdateItem',
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    test('grants CloudWatch Logs permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'CloudWatchLogsForTraining',
              Action: [
                'logs:CreateLogGroup',
                'logs:CreateLogStream',
                'logs:PutLogEvents',
              ],
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });

    test('grants S3 read/write on fine-tuning data bucket', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith(['s3:GetObject*', 's3:GetBucket*', 's3:List*']),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  // ============================================================
  // Security Group
  // ============================================================

  describe('SageMaker Security Group', () => {
    test('creates security group with correct name', () => {
      template.hasResourceProperties('AWS::EC2::SecurityGroup', {
        GroupDescription: 'Security group for SageMaker training jobs - outbound HTTPS only',
        GroupName: 'test-project-sagemaker-sg',
      });
    });

    test('configures outbound HTTPS (port 443) only', () => {
      template.hasResourceProperties('AWS::EC2::SecurityGroup', {
        SecurityGroupEgress: [
          {
            CidrIp: '0.0.0.0/0',
            Description: 'Allow outbound HTTPS for AWS service access and model downloads',
            FromPort: 443,
            IpProtocol: 'tcp',
            ToPort: 443,
          },
        ],
      });
    });
  });

  // ============================================================
  // SSM Parameter Exports
  // ============================================================

  describe('SSM Parameter Exports', () => {
    test('exports jobs table name', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/jobs-table-name',
        Type: 'String',
      });
    });

    test('exports jobs table ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/jobs-table-arn',
        Type: 'String',
      });
    });

    test('exports access table name', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/access-table-name',
        Type: 'String',
      });
    });

    test('exports access table ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/access-table-arn',
        Type: 'String',
      });
    });

    test('exports data bucket name', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/data-bucket-name',
        Type: 'String',
      });
    });

    test('exports data bucket ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/data-bucket-arn',
        Type: 'String',
      });
    });

    test('exports SageMaker execution role ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/sagemaker-execution-role-arn',
        Type: 'String',
      });
    });

    test('exports SageMaker security group ID', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/sagemaker-security-group-id',
        Type: 'String',
      });
    });

    test('exports private subnet IDs', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/fine-tuning/private-subnet-ids',
        Type: 'String',
      });
    });

    test('creates all 9 required SSM parameters', () => {
      const ssmParameters = template.findResources('AWS::SSM::Parameter');
      const ftParameters = Object.values(ssmParameters).filter((param: any) =>
        param.Properties.Name.includes('/fine-tuning/')
      );
      expect(ftParameters.length).toBe(9);
    });
  });

  // ============================================================
  // CloudFormation Outputs
  // ============================================================

  describe('CloudFormation Outputs', () => {
    test('outputs jobs table name', () => {
      template.hasOutput('FineTuningJobsTableName', {
        Description: 'Fine-tuning jobs DynamoDB table name',
      });
    });

    test('outputs access table name', () => {
      template.hasOutput('FineTuningAccessTableName', {
        Description: 'Fine-tuning access DynamoDB table name',
      });
    });

    test('outputs data bucket name', () => {
      template.hasOutput('FineTuningDataBucketName', {
        Description: 'Fine-tuning data S3 bucket name',
      });
    });

    test('outputs SageMaker execution role ARN', () => {
      template.hasOutput('SageMakerExecutionRoleArn', {
        Description: 'SageMaker execution role ARN',
      });
    });

    test('outputs SageMaker security group ID', () => {
      template.hasOutput('SageMakerSecurityGroupId', {
        Description: 'SageMaker security group ID',
      });
    });
  });

  // ============================================================
  // Removal Policy
  // ============================================================

  describe('Removal Policy', () => {
    test('sets DESTROY removal policy when retainDataOnDelete is false', () => {
      const resources = template.toJSON().Resources;
      const tables = Object.values(resources).filter(
        (r: any) => r.Type === 'AWS::DynamoDB::Table'
      );

      tables.forEach((table: any) => {
        expect(table.DeletionPolicy).toBe('Delete');
      });
    });

    test('sets RETAIN removal policy when retainDataOnDelete is true', () => {
      const retainConfig = createMockConfig({
        fineTuning: { enabled: true },
        retainDataOnDelete: true,
      });
      const retainApp = new cdk.App();
      mockSsmContext(retainApp, retainConfig, ['SageMakerFineTuningStack']);

      const retainStack = new SageMakerFineTuningStack(retainApp, 'RetainStack', {
        config: retainConfig,
        env: mockEnv(retainConfig),
      });

      const retainTemplate = Template.fromStack(retainStack);
      const resources = retainTemplate.toJSON().Resources;
      const tables = Object.values(resources).filter(
        (r: any) => r.Type === 'AWS::DynamoDB::Table'
      );

      tables.forEach((table: any) => {
        expect(table.DeletionPolicy).toBe('Retain');
      });
    });
  });

  // ============================================================
  // Resource Counts
  // ============================================================

  describe('Resource Counts', () => {
    test('creates expected number of main resources', () => {
      template.resourceCountIs('AWS::DynamoDB::Table', 2);
      template.resourceCountIs('AWS::S3::Bucket', 1);
      template.resourceCountIs('AWS::IAM::Role', 2); // SageMaker exec role + CDK auto-created role for bucket policy
      template.resourceCountIs('AWS::EC2::SecurityGroup', 1);
    });
  });

  // ============================================================
  // Stack Integration
  // ============================================================

  describe('Stack Integration', () => {
    test('stack synthesizes without errors', () => {
      expect(() => app.synth()).not.toThrow();
    });
  });
});
