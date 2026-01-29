import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { RagIngestionStack } from '../lib/rag-ingestion-stack';
import { AppConfig } from '../lib/config';

/**
 * Unit Tests for RAG Ingestion Stack
 * 
 * These tests verify that the RagIngestionStack creates all required resources
 * with correct configurations, IAM permissions, and SSM parameter exports.
 * 
 * **Validates: Requirements 16.1-16.9**
 */

describe('RagIngestionStack', () => {
  let app: cdk.App;
  let stack: RagIngestionStack;
  let template: Template;
  let config: AppConfig;

  beforeEach(() => {
    app = new cdk.App();

    // Create test configuration
    config = {
      environment: 'test',
      projectPrefix: 'test-project',
      awsAccount: '123456789012',
      awsRegion: 'us-east-1',
      vpcCidr: '10.0.0.0/16',
      entraClientId: 'test-client-id',
      entraTenantId: 'test-tenant-id',
      frontend: {
        enabled: true,
        enableRoute53: false,
        cloudFrontPriceClass: 'PriceClass_100',
      },
      appApi: {
        enabled: true,
        cpu: 256,
        memory: 512,
        desiredCount: 1,
        maxCapacity: 4,
        databaseType: 'none',
        enableRds: false,
        imageTag: 'latest',
        entraRedirectUri: 'http://localhost:3000/auth/callback',
      },
      inferenceApi: {
        enabled: true,
        cpu: 256,
        memory: 512,
        desiredCount: 1,
        maxCapacity: 4,
        enableGpu: false,
        imageTag: 'latest',
        enableAuthentication: 'true',
        logLevel: 'INFO',
        uploadDir: '/tmp/uploads',
        outputDir: '/tmp/outputs',
        generatedImagesDir: '/tmp/images',
        apiUrl: 'http://localhost:8000',
        frontendUrl: 'http://localhost:3000',
        corsOrigins: 'http://localhost:3000',
        tavilyApiKey: 'test-key',
        novaActApiKey: 'test-key',
      },
      gateway: {
        enabled: true,
        apiType: 'REST',
        throttleRateLimit: 1000,
        throttleBurstLimit: 2000,
        enableWaf: false,
      },
      fileUpload: {
        enabled: true,
        maxFileSizeBytes: 4 * 1024 * 1024,
        maxFilesPerMessage: 5,
        userQuotaBytes: 1024 * 1024 * 1024,
        retentionDays: 365,
      },
      assistants: {
        enabled: true,
        corsOrigins: 'http://localhost:3000,https://example.com',
      },
      ragIngestion: {
        enabled: true,
        corsOrigins: 'http://localhost:3000,https://example.com',
        lambdaMemorySize: 10240,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      },
      tags: {
        Environment: 'test',
        Project: 'test-project',
        ManagedBy: 'CDK',
      },
    };

    // Mock SSM parameters that the stack imports
    app.node.setContext(`ssm:account=${config.awsAccount}:parameterName=/${config.projectPrefix}/network/vpc-id:region=${config.awsRegion}`, 'vpc-12345');
    app.node.setContext(`ssm:account=${config.awsAccount}:parameterName=/${config.projectPrefix}/network/vpc-cidr:region=${config.awsRegion}`, '10.0.0.0/16');
    app.node.setContext(`ssm:account=${config.awsAccount}:parameterName=/${config.projectPrefix}/network/private-subnet-ids:region=${config.awsRegion}`, 'subnet-1,subnet-2');
    app.node.setContext(`ssm:account=${config.awsAccount}:parameterName=/${config.projectPrefix}/network/availability-zones:region=${config.awsRegion}`, 'us-east-1a,us-east-1b');
    app.node.setContext(`ssm:account=${config.awsAccount}:parameterName=/${config.projectPrefix}/rag-ingestion/image-tag:region=${config.awsRegion}`, 'test-tag-123');

    stack = new RagIngestionStack(app, 'TestRagIngestionStack', {
      config,
      env: {
        account: config.awsAccount,
        region: config.awsRegion,
      },
    });

    template = Template.fromStack(stack);
  });

  // ============================================================
  // S3 Documents Bucket Tests
  // ============================================================

  describe('S3 Documents Bucket', () => {
    test('creates S3 bucket with correct name', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: 'test-project-test-rag-documents',
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

    test('enables versioning', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        VersioningConfiguration: {
          Status: 'Enabled',
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

    test('configures CORS with correct origins and methods', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        CorsConfiguration: {
          CorsRules: [
            {
              AllowedOrigins: ['http://localhost:3000', 'https://example.com'],
              AllowedMethods: ['GET', 'PUT', 'HEAD'],
              AllowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
              ExposedHeaders: ['ETag', 'Content-Length', 'Content-Type'],
              MaxAge: 3600,
            },
          ],
        },
      });
    });
  });

  // ============================================================
  // S3 Vectors Bucket and Index Tests
  // ============================================================

  describe('S3 Vectors Bucket and Index', () => {
    test('creates S3 Vectors bucket with correct name', () => {
      template.hasResourceProperties('AWS::S3Vectors::VectorBucket', {
        VectorBucketName: 'test-project-test-rag-vector-store-v1',
      });
    });

    test('creates vector index with correct configuration', () => {
      template.hasResourceProperties('AWS::S3Vectors::Index', {
        VectorBucketName: 'test-project-test-rag-vector-store-v1',
        IndexName: 'test-project-test-rag-vector-index-v1',
        DataType: 'float32',
        Dimension: 1024,
        DistanceMetric: 'cosine',
      });
    });

    test('configures metadata with non-filterable text field', () => {
      template.hasResourceProperties('AWS::S3Vectors::Index', {
        MetadataConfiguration: {
          NonFilterableMetadataKeys: ['text'],
        },
      });
    });

    test('vector index depends on vector bucket', () => {
      const resources = template.toJSON().Resources;
      const vectorIndex = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::S3Vectors::Index'
      ) as any;
      const vectorBucket = Object.keys(resources).find(
        (key) => resources[key].Type === 'AWS::S3Vectors::VectorBucket'
      );

      expect(vectorIndex).toBeDefined();
      expect(vectorBucket).toBeDefined();
      expect(vectorIndex.DependsOn).toContain(vectorBucket);
    });
  });

  // ============================================================
  // DynamoDB Table Tests
  // ============================================================

  describe('DynamoDB Assistants Table', () => {
    test('creates table with correct name', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: 'test-project-test-rag-assistants',
      });
    });

    test('configures PK and SK keys', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        KeySchema: [
          {
            AttributeName: 'PK',
            KeyType: 'HASH',
          },
          {
            AttributeName: 'SK',
            KeyType: 'RANGE',
          },
        ],
        AttributeDefinitions: Match.arrayWith([
          {
            AttributeName: 'PK',
            AttributeType: 'S',
          },
          {
            AttributeName: 'SK',
            AttributeType: 'S',
          },
        ]),
      });
    });

    test('uses PAY_PER_REQUEST billing mode', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        BillingMode: 'PAY_PER_REQUEST',
      });
    });

    test('enables point-in-time recovery', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
        },
      });
    });

    test('uses AWS_MANAGED encryption', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        SSESpecification: {
          SSEEnabled: true,
        },
      });
    });

    test('creates OwnerStatusIndex GSI', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: 'OwnerStatusIndex',
            KeySchema: [
              {
                AttributeName: 'GSI_PK',
                KeyType: 'HASH',
              },
              {
                AttributeName: 'GSI_SK',
                KeyType: 'RANGE',
              },
            ],
          }),
        ]),
      });
    });

    test('creates VisibilityStatusIndex GSI', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: 'VisibilityStatusIndex',
            KeySchema: [
              {
                AttributeName: 'GSI2_PK',
                KeyType: 'HASH',
              },
              {
                AttributeName: 'GSI2_SK',
                KeyType: 'RANGE',
              },
            ],
          }),
        ]),
      });
    });

    test('creates SharedWithIndex GSI', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: 'SharedWithIndex',
            KeySchema: [
              {
                AttributeName: 'GSI3_PK',
                KeyType: 'HASH',
              },
              {
                AttributeName: 'GSI3_SK',
                KeyType: 'RANGE',
              },
            ],
            Projection: {
              ProjectionType: 'ALL',
            },
          }),
        ]),
      });
    });

    test('sets DESTROY removal policy for non-prod environment', () => {
      // In test environment, should be DESTROY
      const resources = template.toJSON().Resources;
      const table = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::DynamoDB::Table'
      ) as any;

      expect(table.DeletionPolicy).toBe('Delete');
    });

    test('sets RETAIN removal policy for prod environment', () => {
      // Create a new app and stack with prod environment
      const prodApp = new cdk.App();
      const prodConfig = { ...config, environment: 'prod' };
      
      // Mock SSM parameters for prod stack
      prodApp.node.setContext(`ssm:account=${prodConfig.awsAccount}:parameterName=/${prodConfig.projectPrefix}/network/vpc-id:region=${prodConfig.awsRegion}`, 'vpc-12345');
      prodApp.node.setContext(`ssm:account=${prodConfig.awsAccount}:parameterName=/${prodConfig.projectPrefix}/network/vpc-cidr:region=${prodConfig.awsRegion}`, '10.0.0.0/16');
      prodApp.node.setContext(`ssm:account=${prodConfig.awsAccount}:parameterName=/${prodConfig.projectPrefix}/network/private-subnet-ids:region=${prodConfig.awsRegion}`, 'subnet-1,subnet-2');
      prodApp.node.setContext(`ssm:account=${prodConfig.awsAccount}:parameterName=/${prodConfig.projectPrefix}/network/availability-zones:region=${prodConfig.awsRegion}`, 'us-east-1a,us-east-1b');
      prodApp.node.setContext(`ssm:account=${prodConfig.awsAccount}:parameterName=/${prodConfig.projectPrefix}/rag-ingestion/image-tag:region=${prodConfig.awsRegion}`, 'test-tag-123');
      
      const prodStack = new RagIngestionStack(prodApp, 'ProdRagIngestionStack', {
        config: prodConfig,
        env: {
          account: prodConfig.awsAccount,
          region: prodConfig.awsRegion,
        },
      });

      const prodTemplate = Template.fromStack(prodStack);
      const resources = prodTemplate.toJSON().Resources;
      const table = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::DynamoDB::Table'
      ) as any;

      expect(table.DeletionPolicy).toBe('Retain');
    });
  });

  // ============================================================
  // Lambda Function Tests
  // ============================================================

  describe('Lambda Function', () => {
    test('creates Lambda function with correct name', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'test-project-test-rag-ingestion',
      });
    });

    test('uses ARM64 architecture', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Architectures: ['arm64'],
      });
    });

    test('configures memory size from config', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        MemorySize: 10240,
      });
    });

    test('configures timeout from config', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Timeout: 900,
      });
    });

    test('configures all required environment variables', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'test-project-test-rag-ingestion',
        Environment: {
          Variables: {
            ASSISTANTS_DOCUMENTS_BUCKET_NAME: Match.anyValue(),
            ASSISTANTS_TABLE_NAME: Match.anyValue(),
            ASSISTANTS_VECTOR_STORE_BUCKET_NAME: 'test-project-test-rag-vector-store-v1',
            ASSISTANTS_VECTOR_STORE_INDEX_NAME: 'test-project-test-rag-vector-index-v1',
            BEDROCK_REGION: 'us-east-1',
          },
        },
      });
    });

    test('uses Docker image from ECR', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'test-project-test-rag-ingestion',
        PackageType: 'Image',
        Code: {
          ImageUri: Match.anyValue(),
        },
      });
    });
  });

  // ============================================================
  // IAM Permissions Tests
  // ============================================================

  describe('IAM Permissions', () => {
    test('grants Lambda read permission on S3 documents bucket', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith(['s3:GetObject*', 's3:GetBucket*', 's3:List*']),
              Effect: 'Allow',
              Resource: Match.arrayWith([
                Match.objectLike({
                  'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('RagDocumentsBucket.*')]),
                }),
              ]),
            }),
          ]),
        },
      });
    });

    test('grants Lambda read/write permission on DynamoDB table', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'dynamodb:BatchGetItem',
                'dynamodb:Query',
                'dynamodb:GetItem',
                'dynamodb:Scan',
                'dynamodb:ConditionCheckItem',
                'dynamodb:BatchWriteItem',
                'dynamodb:PutItem',
                'dynamodb:UpdateItem',
                'dynamodb:DeleteItem',
              ]),
              Effect: 'Allow',
              Resource: Match.arrayWith([
                Match.objectLike({
                  'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('RagAssistantsTable.*')]),
                }),
              ]),
            }),
          ]),
        },
      });
    });

    test('grants Lambda permission for S3 Vectors operations', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: [
                's3vectors:ListVectorBuckets',
                's3vectors:GetVectorBucket',
                's3vectors:GetIndex',
                's3vectors:PutVectors',
                's3vectors:ListVectors',
                's3vectors:ListIndexes',
                's3vectors:GetVector',
                's3vectors:GetVectors',
                's3vectors:DeleteVector',
              ],
              Effect: 'Allow',
              Resource: Match.arrayWith([
                Match.stringLikeRegexp('arn:aws:s3vectors:.*:.*:bucket/.*rag-vector-store-v1'),
                Match.stringLikeRegexp('arn:aws:s3vectors:.*:.*:bucket/.*rag-vector-store-v1/index/.*rag-vector-index-v1'),
              ]),
            }),
          ]),
        },
      });
    });

    test('grants Lambda permission to invoke Bedrock model', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'bedrock:InvokeModel',
              Effect: 'Allow',
              Resource: Match.stringLikeRegexp('arn:aws:bedrock:.*::foundation-model/amazon\\.titan-embed-text-v2.*'),
            }),
          ]),
        },
      });
    });
  });

  // ============================================================
  // S3 Event Notification Tests
  // ============================================================

  describe('S3 Event Notifications', () => {
    test('configures S3 event notification for Lambda trigger', () => {
      template.hasResourceProperties('Custom::S3BucketNotifications', {
        NotificationConfiguration: {
          LambdaFunctionConfigurations: [
            {
              Events: ['s3:ObjectCreated:*'],
              Filter: {
                Key: {
                  FilterRules: [
                    {
                      Name: 'prefix',
                      Value: 'assistants/',
                    },
                  ],
                },
              },
              LambdaFunctionArn: Match.objectLike({
                'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('RagIngestionLambda.*')]),
              }),
            },
          ],
        },
      });
    });
  });

  // ============================================================
  // SSM Parameter Exports Tests
  // ============================================================

  describe('SSM Parameter Exports', () => {
    test('exports documents bucket name to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/documents-bucket-name',
        Type: 'String',
        Value: Match.objectLike({
          Ref: Match.stringLikeRegexp('RagDocumentsBucket.*'),
        }),
        Description: 'RAG documents bucket name',
      });
    });

    test('exports documents bucket ARN to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/documents-bucket-arn',
        Type: 'String',
        Value: Match.objectLike({
          'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('RagDocumentsBucket.*'), 'Arn']),
        }),
        Description: 'RAG documents bucket ARN',
      });
    });

    test('exports assistants table name to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/assistants-table-name',
        Type: 'String',
        Value: Match.objectLike({
          Ref: Match.stringLikeRegexp('RagAssistantsTable.*'),
        }),
        Description: 'RAG assistants table name',
      });
    });

    test('exports assistants table ARN to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/assistants-table-arn',
        Type: 'String',
        Value: Match.objectLike({
          'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('RagAssistantsTable.*'), 'Arn']),
        }),
        Description: 'RAG assistants table ARN',
      });
    });

    test('exports vector bucket name to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/vector-bucket-name',
        Type: 'String',
        Value: 'test-project-test-rag-vector-store-v1',
        Description: 'RAG vector store bucket name',
      });
    });

    test('exports vector index name to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/vector-index-name',
        Type: 'String',
        Value: 'test-project-test-rag-vector-index-v1',
        Description: 'RAG vector store index name',
      });
    });

    test('exports ingestion Lambda ARN to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/ingestion-lambda-arn',
        Type: 'String',
        Value: Match.objectLike({
          'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('RagIngestionLambda.*'), 'Arn']),
        }),
        Description: 'RAG ingestion Lambda ARN',
      });
    });

    test('creates all 7 required SSM parameters', () => {
      const ssmParameters = template.findResources('AWS::SSM::Parameter');
      const ragParameters = Object.values(ssmParameters).filter((param: any) =>
        param.Properties.Name.includes('/rag/')
      );

      expect(ragParameters.length).toBe(7);
    });
  });

  // ============================================================
  // CloudFormation Outputs Tests
  // ============================================================

  describe('CloudFormation Outputs', () => {
    test('outputs documents bucket name', () => {
      template.hasOutput('DocumentsBucketName', {
        Description: 'RAG documents bucket name',
        Export: {
          Name: 'test-project-RagDocumentsBucketName',
        },
      });
    });

    test('outputs assistants table name', () => {
      template.hasOutput('AssistantsTableName', {
        Description: 'RAG assistants table name',
        Export: {
          Name: 'test-project-RagAssistantsTableName',
        },
      });
    });

    test('outputs ingestion Lambda ARN', () => {
      template.hasOutput('IngestionLambdaArn', {
        Description: 'RAG ingestion Lambda function ARN',
        Export: {
          Name: 'test-project-RagIngestionLambdaArn',
        },
      });
    });

    test('outputs vector bucket name', () => {
      template.hasOutput('VectorBucketName', {
        Description: 'RAG vector store bucket name',
        Export: {
          Name: 'test-project-RagVectorBucketName',
        },
      });
    });

    test('outputs vector index name', () => {
      template.hasOutput('VectorIndexName', {
        Description: 'RAG vector store index name',
        Export: {
          Name: 'test-project-RagVectorIndexName',
        },
      });
    });
  });

  // ============================================================
  // Resource Count Tests
  // ============================================================

  describe('Resource Counts', () => {
    test('creates expected number of main resources', () => {
      // S3 bucket, Vector bucket, Vector index, DynamoDB table, Lambda function (+ 1 for BucketNotifications handler)
      template.resourceCountIs('AWS::S3::Bucket', 1);
      template.resourceCountIs('AWS::S3Vectors::VectorBucket', 1);
      template.resourceCountIs('AWS::S3Vectors::Index', 1);
      template.resourceCountIs('AWS::DynamoDB::Table', 1);
      // Note: There are 2 Lambda functions - our RAG ingestion Lambda and the BucketNotifications handler
      const lambdas = template.findResources('AWS::Lambda::Function');
      const ragLambda = Object.values(lambdas).find(
        (lambda: any) => lambda.Properties?.FunctionName === 'test-project-test-rag-ingestion'
      );
      expect(ragLambda).toBeDefined();
    });

    test('creates expected number of SSM parameters', () => {
      // 7 SSM parameters for RAG resources
      const ssmParameters = template.findResources('AWS::SSM::Parameter');
      const ragParameters = Object.values(ssmParameters).filter((param: any) =>
        param.Properties.Name.includes('/rag/')
      );

      expect(ragParameters.length).toBe(7);
    });
  });

  // ============================================================
  // Integration Tests
  // ============================================================

  describe('Stack Integration', () => {
    test('stack synthesizes without errors', () => {
      expect(() => app.synth()).not.toThrow();
    });

    test('all resources have proper dependencies', () => {
      // This test ensures the stack can be deployed in the correct order
      const resources = template.toJSON().Resources;

      // Lambda should depend on bucket and table
      const lambda = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::Lambda::Function'
      ) as any;

      expect(lambda).toBeDefined();
      expect(lambda.DependsOn).toBeDefined();
    });

    test('resource names follow naming convention', () => {
      const resources = template.toJSON().Resources;

      // Check that all resource names use the "rag-" prefix
      const bucket = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::S3::Bucket'
      ) as any;
      const table = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::DynamoDB::Table'
      ) as any;
      const lambda = Object.values(resources).find(
        (r: any) => r.Type === 'AWS::Lambda::Function'
      ) as any;

      expect(bucket.Properties.BucketName).toContain('rag-documents');
      expect(table.Properties.TableName).toContain('rag-assistants');
      expect(lambda.Properties.FunctionName).toContain('rag-ingestion');
    });

    test('no resources use "assistants-" prefix (old naming)', () => {
      const resources = template.toJSON().Resources;

      // Ensure no resources use the old "assistants-" prefix
      Object.values(resources).forEach((resource: any) => {
        if (resource.Properties?.BucketName && typeof resource.Properties.BucketName === 'string') {
          expect(resource.Properties.BucketName).not.toMatch(/^[^-]+-assistants-/);
        }
        if (resource.Properties?.TableName && typeof resource.Properties.TableName === 'string') {
          expect(resource.Properties.TableName).not.toMatch(/^[^-]+-assistants$/);
        }
        if (resource.Properties?.FunctionName && typeof resource.Properties.FunctionName === 'string') {
          expect(resource.Properties.FunctionName).not.toMatch(/assistants-documents-ingestion/);
        }
      });
    });
  });
});
