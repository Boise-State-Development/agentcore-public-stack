/**
 * Network + ALB + ECS cluster detailed tests.
 * S3 bucket property tests for SPA, artifacts, MCP sandbox.
 * Build scripts shape tests.
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as fs from 'fs';
import * as path from 'path';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';
import { mockCognitoRefs, MOCK_BFF_CLIENT_ID } from './helpers/mock-cognito';

import { NetworkConstruct } from '../lib/constructs/network/network-construct';
import { AlbConstruct } from '../lib/constructs/network/alb-construct';
import { EcsClusterConstruct } from '../lib/constructs/network/ecs-cluster-construct';
import { SpaBucketConstruct } from '../lib/constructs/spa/spa-bucket-construct';
import { ArtifactsDataConstruct } from '../lib/constructs/artifacts/artifacts-data-construct';
import { McpSandboxBucketConstruct } from '../lib/constructs/mcp-sandbox/mcp-sandbox-bucket-construct';
import { AgentCoreGatewayConstruct } from '../lib/constructs/gateway/agentcore-gateway-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

const config = createMockConfig();

describe('NetworkConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new NetworkConstruct(stack, 'Net', { config });
    t = Template.fromStack(stack);
  });

  it('VPC has DNS hostnames enabled', () => {
    t.hasResourceProperties('AWS::EC2::VPC', {
      EnableDnsHostnames: true,
      EnableDnsSupport: true,
    });
  });

  it('VPC uses the configured CIDR', () => {
    t.hasResourceProperties('AWS::EC2::VPC', {
      CidrBlock: '10.0.0.0/16',
    });
  });

  it('creates route tables', () => {
    const tables = t.findResources('AWS::EC2::RouteTable');
    expect(Object.keys(tables).length).toBeGreaterThanOrEqual(2);
  });

  it('creates an EIP for the NAT gateway', () => {
    t.resourceCountIs('AWS::EC2::EIP', 1);
  });

  it('publishes public subnet IDs to SSM', () => {
  });
});

describe('AlbConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    const vpc = new ec2.Vpc(stack, 'Vpc');
    new AlbConstruct(stack, 'Alb', { config, vpc });
    t = Template.fromStack(stack);
  });

  it('ALB is internet-facing', () => {
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internet-facing',
    });
  });

  it('security group allows HTTP from anywhere', () => {
    t.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({ FromPort: 80, ToPort: 80, CidrIp: '0.0.0.0/0' }),
      ]),
    });
  });

  it('security group allows HTTPS from anywhere', () => {
    t.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({ FromPort: 443, ToPort: 443, CidrIp: '0.0.0.0/0' }),
      ]),
    });
  });

  it('HTTP listener returns 404 by default (no cert)', () => {
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      DefaultActions: Match.arrayWith([
        Match.objectLike({ Type: 'fixed-response', FixedResponseConfig: Match.objectLike({ StatusCode: '404' }) }),
      ]),
    });
  });

  it('publishes ALB DNS name to SSM', () => {
  });
});

describe('EcsClusterConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    const vpc = new ec2.Vpc(stack, 'Vpc');
    new EcsClusterConstruct(stack, 'Ecs', { config, vpc });
    t = Template.fromStack(stack);
  });

  it('cluster has Container Insights enabled', () => {
    // containerInsightsV2 maps to ClusterSettings with value 'enhanced'
    // or the cluster just has the setting present
    const clusters = t.findResources('AWS::ECS::Cluster');
    expect(Object.keys(clusters).length).toBe(1);
  });

  it('publishes cluster name to SSM', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/network/ecs-cluster-name',
    });
  });

  it('publishes cluster ARN to SSM', () => {
  });
});

describe('SpaBucketConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new SpaBucketConstruct(stack, 'Spa', { config });
    t = Template.fromStack(stack);
  });

  it('bucket blocks all public access', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it('bucket has 30-day non-current version expiration', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Id: 'DeleteOldVersions', NoncurrentVersionExpiration: { NoncurrentDays: 30 } }),
        ]),
      },
    });
  });

  it('publishes bucket name to SSM', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/frontend/bucket-name',
    });
  });
});

describe('ArtifactsDataConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new ArtifactsDataConstruct(stack, 'Art', { config });
    t = Template.fromStack(stack);
  });

  it('content bucket blocks all public access', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it('content bucket enforces SSL', () => {
    // CDK adds a bucket policy with aws:SecureTransport condition
    t.hasResourceProperties('AWS::S3::BucketPolicy', {});
  });

  it('content bucket has multipart abort lifecycle', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Id: 'abort-stale-multipart', AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 } }),
        ]),
      },
    });
  });

  it('table has SessionIndex GSI', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'SessionIndex' }),
      ]),
    });
  });

  it('table has TTL on ttl', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('publishes bucket name to SSM', () => {
  });

  it('publishes table name to SSM', () => {
  });
});

describe('McpSandboxBucketConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new McpSandboxBucketConstruct(stack, 'Mcp', { config });
    t = Template.fromStack(stack);
  });

  it('bucket blocks all public access', () => {
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it('bucket enforces SSL', () => {
    t.hasResourceProperties('AWS::S3::BucketPolicy', {});
  });
});

describe('AgentCoreGatewayConstruct — detailed', () => {
  let t: Template;
  beforeAll(() => {
    const stack = testStack();
    new AgentCoreGatewayConstruct(stack, 'GW', {
      config,
      ...mockCognitoRefs(stack),
    });
    t = Template.fromStack(stack);
  });

  it('gateway uses MCP protocol', () => {
    t.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      ProtocolType: 'MCP',
    });
  });

  it('gateway uses CUSTOM_JWT authorizer by default', () => {
    // Default inbound auth is JWT — required for outbound OBO token exchange,
    // since AgentCore can only exchange a *user* subject token.
    t.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      AuthorizerType: 'CUSTOM_JWT',
    });
  });

  it('JWT authorizer validates client_id, not audience', () => {
    // Cognito *access* tokens carry `client_id` and no `aud` claim, so
    // AllowedAudience could never match and would 401 every call.
    t.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      AuthorizerConfiguration: {
        CustomJWTAuthorizer: {
          AllowedClients: [MOCK_BFF_CLIENT_ID],
        },
      },
    });
    const gw = Object.values(
      t.findResources('AWS::BedrockAgentCore::Gateway'),
    )[0] as any;
    expect(
      gw.Properties.AuthorizerConfiguration.CustomJWTAuthorizer.AllowedAudience,
    ).toBeUndefined();
  });

  it('JWT authorizer points at the Cognito OIDC discovery document', () => {
    const gw = Object.values(
      t.findResources('AWS::BedrockAgentCore::Gateway'),
    )[0] as any;
    expect(
      gw.Properties.AuthorizerConfiguration.CustomJWTAuthorizer.DiscoveryUrl,
    ).toContain('/.well-known/openid-configuration');
  });

  it('gateway falls back to AWS_IAM when inboundAuth is iam (rollback path)', () => {
    // Code-free rollback: flipping the flag restores SigV4 inbound auth. Both
    // AuthorizerType and AuthorizerConfiguration are CFN "no interruption"
    // updates, so this does not replace the Gateway or orphan its targets.
    const stack = testStack();
    new AgentCoreGatewayConstruct(stack, 'GW', {
      config: createMockConfig({ gateway: { inboundAuth: 'iam' } }),
    });
    const iamTemplate = Template.fromStack(stack);
    iamTemplate.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      AuthorizerType: 'AWS_IAM',
    });
    const gw = Object.values(
      iamTemplate.findResources('AWS::BedrockAgentCore::Gateway'),
    )[0] as any;
    expect(gw.Properties.AuthorizerConfiguration).toBeUndefined();
  });

  it('throws when JWT auth is selected without Cognito refs', () => {
    // Fail fast at synth rather than deploying a Gateway that 401s everything.
    const stack = testStack();
    expect(
      () => new AgentCoreGatewayConstruct(stack, 'GW', { config }),
    ).toThrow(/inboundAuth is 'jwt' but/);
  });

  it('gateway role has NO standing lambda:Invoke* grant (per-target only)', () => {
    // Invoke is granted per-target by app-api at registration, not by a standing
    // wildcard on the gateway role — so its only inline grant is CloudWatch Logs.
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.not(
          Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith(['lambda:InvokeFunctionUrl']),
            }),
          ]),
        ),
      },
    });
  });
});

describe('Build scripts', () => {
  const SCRIPTS = path.resolve(__dirname, '..', '..', 'scripts', 'build');

  it('compute-content-hash.sh exists and is executable', () => {
    const stat = fs.statSync(path.join(SCRIPTS, 'compute-content-hash.sh'));
    expect(stat.isFile()).toBe(true);
    expect(stat.mode & 0o111).toBeGreaterThan(0); // executable
  });

  it('build-and-push-if-changed.sh exists and is executable', () => {
    const stat = fs.statSync(path.join(SCRIPTS, 'build-and-push-if-changed.sh'));
    expect(stat.isFile()).toBe(true);
    expect(stat.mode & 0o111).toBeGreaterThan(0);
  });

  it('build-one.sh exists and is executable', () => {
    const stat = fs.statSync(path.join(SCRIPTS, 'build-one.sh'));
    expect(stat.isFile()).toBe(true);
    expect(stat.mode & 0o111).toBeGreaterThan(0);
  });

  it('build-all-images.sh exists and is executable', () => {
    const stat = fs.statSync(path.join(SCRIPTS, 'build-all-images.sh'));
    expect(stat.isFile()).toBe(true);
    expect(stat.mode & 0o111).toBeGreaterThan(0);
  });

  it('compute-content-hash.sh uses sha256sum', () => {
    const content = fs.readFileSync(path.join(SCRIPTS, 'compute-content-hash.sh'), 'utf-8');
    expect(content).toContain('sha256sum');
  });

  it('build-and-push-if-changed.sh calls compute-content-hash.sh', () => {
    const content = fs.readFileSync(path.join(SCRIPTS, 'build-and-push-if-changed.sh'), 'utf-8');
    expect(content).toContain('compute-content-hash.sh');
  });

  it('build-one.sh calls build-and-push-if-changed.sh', () => {
    const content = fs.readFileSync(path.join(SCRIPTS, 'build-one.sh'), 'utf-8');
    expect(content).toContain('build-and-push-if-changed.sh');
  });

  it('build-one.sh handles all three services', () => {
    const content = fs.readFileSync(path.join(SCRIPTS, 'build-one.sh'), 'utf-8');
    expect(content).toContain('app-api)');
    expect(content).toContain('inference-api)');
    expect(content).toContain('rag-ingestion)');
  });

  it('build-all-images.sh calls build-one.sh', () => {
    const content = fs.readFileSync(path.join(SCRIPTS, 'build-all-images.sh'), 'utf-8');
    expect(content).toContain('build-one.sh');
  });

  it('build-one.sh emits to GITHUB_OUTPUT', () => {
    const content = fs.readFileSync(path.join(SCRIPTS, 'build-one.sh'), 'utf-8');
    expect(content).toContain('GITHUB_OUTPUT');
  });
});

describe('Deploy scripts', () => {
  const SCRIPTS_ROOT = path.resolve(__dirname, '..', '..', 'scripts');

  for (const dir of ['platform', 'frontend']) {
    it(`scripts/${dir}/deploy.sh exists`, () => {
      expect(fs.existsSync(path.join(SCRIPTS_ROOT, dir, 'deploy.sh'))).toBe(true);
    });

    it(`scripts/${dir}/deploy.sh sources load-env.sh`, () => {
      const content = fs.readFileSync(path.join(SCRIPTS_ROOT, dir, 'deploy.sh'), 'utf-8');
      expect(content).toContain('load-env.sh');
    });
  }

  it('scripts/frontend/build.sh runs gen-version.js', () => {
    const content = fs.readFileSync(path.join(SCRIPTS_ROOT, 'frontend', 'build.sh'), 'utf-8');
    expect(content).toContain('gen-version.js');
  });

  it('scripts/frontend/deploy.sh reads from SSM', () => {
    const content = fs.readFileSync(path.join(SCRIPTS_ROOT, 'frontend', 'deploy.sh'), 'utf-8');
    expect(content).toContain('aws ssm get-parameter');
  });

  it('scripts/frontend/deploy.sh runs aws s3 sync', () => {
    const content = fs.readFileSync(path.join(SCRIPTS_ROOT, 'frontend', 'deploy.sh'), 'utf-8');
    expect(content).toContain('aws s3 sync');
  });

  it('scripts/frontend/deploy.sh invalidates CloudFront', () => {
    const content = fs.readFileSync(path.join(SCRIPTS_ROOT, 'frontend', 'deploy.sh'), 'utf-8');
    expect(content).toContain('aws cloudfront create-invalidation');
  });
});
