/**
 * Per-construct unit tests for Platform constructs.
 *
 * Each construct is instantiated in isolation inside a test stack and
 * verified for correct resource creation.
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

import { NetworkConstruct } from '../lib/constructs/network/network-construct';
import { AlbConstruct } from '../lib/constructs/network/alb-construct';
import { EcsClusterConstruct } from '../lib/constructs/network/ecs-cluster-construct';
import { AuthSecretConstruct } from '../lib/constructs/identity/auth-secret-construct';
import { PlatformIdentityConstruct } from '../lib/constructs/identity/platform-identity-construct';
import { BffCookieKeyConstruct } from '../lib/constructs/identity/bff-cookie-key-construct';
import { VoiceTicketConstruct } from '../lib/constructs/identity/voice-ticket-construct';
import { CognitoConstruct } from '../lib/constructs/identity/cognito-construct';
import { OAuthTablesConstruct } from '../lib/constructs/identity/oauth-tables-construct';
import { AuthProvidersConstruct } from '../lib/constructs/identity/auth-providers-construct';
import { AuthTablesConstruct } from '../lib/constructs/data/auth-tables-construct';
import { QuotaTablesConstruct } from '../lib/constructs/data/quota-tables-construct';
import { CostTrackingTablesConstruct } from '../lib/constructs/data/cost-tracking-tables-construct';
import { AdminTablesConstruct } from '../lib/constructs/data/admin-tables-construct';
import { FileUploadConstruct } from '../lib/constructs/data/file-upload-construct';
import { SharedConversationsConstruct } from '../lib/constructs/data/shared-conversations-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { FineTuningDataConstruct } from '../lib/constructs/fine-tuning/fine-tuning-data-construct';
import { ArtifactsDataConstruct } from '../lib/constructs/artifacts/artifacts-data-construct';
import { SkillResourcesConstruct } from '../lib/constructs/skills/skill-resources-construct';
import { SpaBucketConstruct } from '../lib/constructs/spa/spa-bucket-construct';
import { AgentCoreGatewayConstruct } from '../lib/constructs/gateway/agentcore-gateway-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

describe('NetworkConstruct', () => {
  it('creates VPC with 4 subnets and 1 NAT', () => {
    const stack = testStack();
    const config = createMockConfig();
    new NetworkConstruct(stack, 'Net', { config });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::EC2::VPC', 1);
    t.resourceCountIs('AWS::EC2::Subnet', 4);
    t.resourceCountIs('AWS::EC2::NatGateway', 1);
  });

  it('publishes VPC SSM parameters', () => {
    const stack = testStack();
    const config = createMockConfig();
    new NetworkConstruct(stack, 'Net', { config });
    const t = Template.fromStack(stack);
  });

  it('exposes vpc property', () => {
    const stack = testStack();
    const config = createMockConfig();
    const net = new NetworkConstruct(stack, 'Net', { config });
    expect(net.vpc).toBeDefined();
    expect(net.vpc.vpcId).toBeDefined();
  });
});

describe('AlbConstruct', () => {
  it('creates ALB + security group + listener', () => {
    const stack = testStack();
    const config = createMockConfig();
    const vpc = new ec2.Vpc(stack, 'Vpc');
    new AlbConstruct(stack, 'Alb', { config, vpc });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    t.resourceCountIs('AWS::EC2::SecurityGroup', 1);
    t.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 1); // HTTP only (no cert)
  });

  it('creates HTTPS listener when certificateArn is set', () => {
    const stack = testStack();
    const config = createMockConfig({ certificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/test' });
    const vpc = new ec2.Vpc(stack, 'Vpc');
    new AlbConstruct(stack, 'Alb', { config, vpc });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 2); // HTTPS + HTTP redirect
  });

  it('publishes ALB SSM parameters', () => {
    const stack = testStack();
    const config = createMockConfig();
    const vpc = new ec2.Vpc(stack, 'Vpc');
    new AlbConstruct(stack, 'Alb', { config, vpc });
    const t = Template.fromStack(stack);
  });
});

describe('EcsClusterConstruct', () => {
  it('creates an ECS cluster', () => {
    const stack = testStack();
    const config = createMockConfig();
    const vpc = new ec2.Vpc(stack, 'Vpc');
    new EcsClusterConstruct(stack, 'Ecs', { config, vpc });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::ECS::Cluster', 1);
  });
});

describe('AuthSecretConstruct', () => {
  it('creates a Secrets Manager secret', () => {
    const stack = testStack();
    new AuthSecretConstruct(stack, 'Auth', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::SecretsManager::Secret', 1);
  });

  it('publishes secret ARN and name to SSM', () => {
    const stack = testStack();
    new AuthSecretConstruct(stack, 'Auth', { config: createMockConfig() });
    const t = Template.fromStack(stack);
  });
});

describe('PlatformIdentityConstruct', () => {
  it('creates a WorkloadIdentity', () => {
    const stack = testStack();
    new PlatformIdentityConstruct(stack, 'Id', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::BedrockAgentCore::WorkloadIdentity', 1);
  });
});

describe('BffCookieKeyConstruct', () => {
  it('creates KMS key + Secrets Manager secret', () => {
    const stack = testStack();
    new BffCookieKeyConstruct(stack, 'Bff', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::KMS::Key', 1);
    t.resourceCountIs('AWS::SecretsManager::Secret', 1);
  });
});

describe('VoiceTicketConstruct', () => {
  it('creates DDB table + secret', () => {
    const stack = testStack();
    new VoiceTicketConstruct(stack, 'Voice', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 1);
    t.resourceCountIs('AWS::SecretsManager::Secret', 1);
  });
});

describe('CognitoConstruct', () => {
  it('creates user pool + client + domain', () => {
    const stack = testStack();
    new CognitoConstruct(stack, 'Cog', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::Cognito::UserPool', 1);
    t.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
    t.resourceCountIs('AWS::Cognito::UserPoolDomain', 1);
  });
});

describe('OAuthTablesConstruct', () => {
  it('creates 2 DDB tables + KMS key + secret', () => {
    const stack = testStack();
    new OAuthTablesConstruct(stack, 'OAuth', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 2);
    t.resourceCountIs('AWS::KMS::Key', 1);
    t.resourceCountIs('AWS::SecretsManager::Secret', 1);
  });
});

describe('AuthProvidersConstruct', () => {
  it('creates DDB table with stream + secret', () => {
    const stack = testStack();
    new AuthProvidersConstruct(stack, 'AP', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 1);
    t.resourceCountIs('AWS::SecretsManager::Secret', 1);
  });
});

describe('AuthTablesConstruct', () => {
  it('creates 5 DDB tables', () => {
    const stack = testStack();
    new AuthTablesConstruct(stack, 'AT', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 5);
  });
});

describe('QuotaTablesConstruct', () => {
  it('creates 2 DDB tables', () => {
    const stack = testStack();
    new QuotaTablesConstruct(stack, 'QT', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 2);
  });
});

describe('CostTrackingTablesConstruct', () => {
  it('creates 4 DDB tables', () => {
    const stack = testStack();
    new CostTrackingTablesConstruct(stack, 'CT', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 4);
  });
});

describe('AdminTablesConstruct', () => {
  it('creates 3 DDB tables', () => {
    const stack = testStack();
    new AdminTablesConstruct(stack, 'Admin', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 3);
  });
});

describe('FileUploadConstruct', () => {
  it('creates S3 bucket + DDB table', () => {
    const stack = testStack();
    new FileUploadConstruct(stack, 'FU', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::S3::Bucket', 1);
    t.resourceCountIs('AWS::DynamoDB::Table', 1);
  });
});

describe('SharedConversationsConstruct', () => {
  it('creates 1 DDB table', () => {
    const stack = testStack();
    new SharedConversationsConstruct(stack, 'SC', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 1);
  });
});

describe('RagDataConstruct', () => {
  it('creates S3 bucket + vector bucket + vector index + DDB table', () => {
    const stack = testStack();
    new RagDataConstruct(stack, 'Rag', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::S3::Bucket', 1);
    t.resourceCountIs('AWS::S3Vectors::VectorBucket', 1);
    t.resourceCountIs('AWS::S3Vectors::Index', 1);
    t.resourceCountIs('AWS::DynamoDB::Table', 1);
  });
});

describe('FineTuningDataConstruct', () => {
  it('creates 2 DDB tables + S3 bucket', () => {
    const stack = testStack();
    new FineTuningDataConstruct(stack, 'FT', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 2);
    t.resourceCountIs('AWS::S3::Bucket', 1);
  });
});

describe('ArtifactsDataConstruct', () => {
  it('creates DDB table + S3 bucket', () => {
    const stack = testStack();
    new ArtifactsDataConstruct(stack, 'Art', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::DynamoDB::Table', 1);
    t.resourceCountIs('AWS::S3::Bucket', 1);
  });
});

describe('SkillResourcesConstruct', () => {
  it('creates a private, encrypted S3 bucket', () => {
    const stack = testStack();
    new SkillResourcesConstruct(stack, 'SkillRes', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::S3::Bucket', 1);
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
        ],
      },
    });
  });
});

describe('SpaBucketConstruct', () => {
  it('creates a versioned S3 bucket', () => {
    const stack = testStack();
    new SpaBucketConstruct(stack, 'Spa', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::S3::Bucket', 1);
    t.hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });
});

describe('AgentCoreGatewayConstruct', () => {
  it('creates Gateway + IAM role', () => {
    const stack = testStack();
    new AgentCoreGatewayConstruct(stack, 'GW', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
    t.resourceCountIs('AWS::IAM::Role', 1);
  });

  it('publishes the gateway id SSM parameter for app-api (issue #419)', () => {
    const stack = testStack();
    new AgentCoreGatewayConstruct(stack, 'GW', { config: createMockConfig() });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/gateway/id',
    });
  });
});
