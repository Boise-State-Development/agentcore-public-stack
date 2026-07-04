import * as cdk from 'aws-cdk-lib';
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
  new KbSyncConstruct(stack, 'KbSync', {
    config,
    assistantsTable: ragData.assistantsTable,
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
});
