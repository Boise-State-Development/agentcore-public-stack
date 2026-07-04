import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { KbSyncConstruct } from '../lib/constructs/kb-sync/kb-sync-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function synth(kbSyncEnabled: boolean): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = createMockConfig({ kbSync: { enabled: kbSyncEnabled } });
  const ragData = new RagDataConstruct(stack, 'RagData', { config });
  const oauthProvidersTable = new dynamodb.Table(stack, 'OAuthProviders', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  });
  new KbSyncConstruct(stack, 'KbSync', {
    config,
    assistantsTable: ragData.assistantsTable,
    documentsBucket: ragData.documentsBucket,
    oauthProvidersTable,
    workloadIdentityName: 'test-platform-workload',
  });
  return Template.fromStack(stack);
}

describe('KbSyncConstruct', () => {
  let t: Template;
  beforeAll(() => {
    t = synth(false);
  });

  it('creates dispatcher and worker Lambdas with distinct command overrides on one image', () => {
    t.hasResourceProperties('AWS::Lambda::Function', {
      ImageConfig: { Command: ['apis.app_api.kb_sync.dispatcher.lambda_handler'] },
      PackageType: 'Image',
    });
    t.hasResourceProperties('AWS::Lambda::Function', {
      ImageConfig: { Command: ['apis.app_api.kb_sync.worker.lambda_handler'] },
      PackageType: 'Image',
    });
  });

  it('single EventBridge rate rule targets the dispatcher', () => {
    t.resourceCountIs('AWS::Events::Rule', 1);
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(15 minutes)',
    });
  });

  it('rule is DISABLED when kbSync.enabled is false (dark by default)', () => {
    t.hasResourceProperties('AWS::Events::Rule', { State: 'DISABLED' });
  });

  it('rule is ENABLED when kbSync.enabled is true', () => {
    const enabled = synth(true);
    enabled.hasResourceProperties('AWS::Events::Rule', { State: 'ENABLED' });
  });

  it('KB_SYNC_ENABLED env mirrors the flag on the dispatcher', () => {
    t.hasResourceProperties('AWS::Lambda::Function', {
      ImageConfig: { Command: ['apis.app_api.kb_sync.dispatcher.lambda_handler'] },
      Environment: {
        Variables: Match.objectLike({
          KB_SYNC_ENABLED: 'false',
          KB_SYNC_WORKER_FUNCTION_NAME: Match.anyValue(),
          DYNAMODB_ASSISTANTS_TABLE_NAME: Match.anyValue(),
        }),
      },
    });
  });

  it('dispatcher may invoke the worker', () => {
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'lambda:InvokeFunction',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('custom metrics are namespace-conditioned', () => {
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'cloudwatch:PutMetricData',
            Condition: { StringEquals: { 'cloudwatch:namespace': 'KBSync' } },
          }),
        ]),
      },
    });
  });

  it('publishes function-name SSM parameters for the code-deploy step', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: Match.stringLikeRegexp('/kb-sync/dispatcher-function-name$'),
    });
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: Match.stringLikeRegexp('/kb-sync/worker-function-name$'),
    });
  });

  it('creates error alarms for both functions', () => {
    t.resourceCountIs('AWS::CloudWatch::Alarm', 2);
  });

  it('worker carries the identity + staging env contract', () => {
    t.hasResourceProperties('AWS::Lambda::Function', {
      ImageConfig: { Command: ['apis.app_api.kb_sync.worker.lambda_handler'] },
      Environment: {
        Variables: Match.objectLike({
          AGENTCORE_RUNTIME_WORKLOAD_NAME: 'test-platform-workload',
          AGENTCORE_LOCAL_OAUTH_CALLBACK_URL: Match.stringLikeRegexp('/oauth-complete$'),
          DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME: Match.anyValue(),
          S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: Match.anyValue(),
        }),
      },
    });
  });

  it('worker may read vault tokens but never complete consent or manage providers', () => {
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AgentCoreVaultTokenRead',
            Action: [
              'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
              'bedrock-agentcore:GetResourceOauth2Token',
            ],
          }),
          // GetResourceOauth2Token reads the vaulted token through the
          // provider's backing Secrets Manager secret — the bedrock-agentcore
          // action is useless without read access to that secret.
          Match.objectLike({
            Sid: 'AgentCoreIdentityOAuthSecrets',
            Action: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
          }),
        ]),
      },
    });
    // The trimmed statement must not quietly grow write-side actions.
    const policies = t.findResources('AWS::IAM::Policy');
    const allActions = JSON.stringify(policies);
    expect(allActions).not.toContain('CompleteResourceTokenAuth');
    expect(allActions).not.toContain('CreateOauth2CredentialProvider');
  });
});
