/**
 * Runtime log retention sweep — the guards that keep every generation of this
 * deployment's AgentCore Runtime log groups expiring, and keep the sweep from
 * reaching anything else.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import { AppConfig } from '../lib/config';
import {
  RuntimeLogRetentionSweepConstruct,
  runtimeLogGroupPrefix,
} from '../lib/constructs/inference-api/runtime-log-retention-sweep-construct';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const RUNTIME_NAME = 'example_agentcore_runtime';
const PREFIX = `/aws/bedrock-agentcore/runtimes/${RUNTIME_NAME}-`;

function synthConstruct(logRetentionDays = 30): Template {
  const base = createMockConfig();
  const config: AppConfig = {
    ...base,
    observability: { ...base.observability, logRetentionDays },
  };
  const stack = new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  new RuntimeLogRetentionSweepConstruct(stack, 'Sweep', { config, agentRuntimeName: RUNTIME_NAME });
  return Template.fromStack(stack);
}

function synthPlatform(runtimeLogRetentionSweepEnabled: boolean): Template {
  const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
  const base = createMockConfig({
    domainName: 'example.com',
    infrastructureHostedZoneDomain: 'example.com',
    certificateArn: cert,
    frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
    artifacts: {
      shareInboxEnabled: false, retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
    mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
  });
  const config: AppConfig = {
    ...base,
    observability: { ...base.observability, runtimeLogRetentionSweepEnabled },
  };
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function statementsWithAction(template: Template, action: string): any[] {
  return Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
    (policy: any) => policy.Properties.PolicyDocument.Statement.filter((s: any) =>
      [s.Action].flat().includes(action)),
  );
}

describe('runtimeLogGroupPrefix', () => {
  it('ends at the name/id separator so it cannot match a longer runtime name', () => {
    expect(runtimeLogGroupPrefix(RUNTIME_NAME)).toBe(PREFIX);
    // Another deployment whose runtime name starts with ours.
    const other = '/aws/bedrock-agentcore/runtimes/example_agentcore_runtime_v2-abcdefghij-DEFAULT';
    expect(other.startsWith(runtimeLogGroupPrefix(RUNTIME_NAME))).toBe(false);
    // A replaced generation of ours, on any endpoint.
    for (const mine of [
      `${PREFIX}abcdefghij-DEFAULT`,
      `${PREFIX}klmnopqrst-DEFAULT`,
      `${PREFIX}abcdefghij-staging`,
    ]) {
      expect(mine.startsWith(runtimeLogGroupPrefix(RUNTIME_NAME))).toBe(true);
    }
  });
});

describe('RuntimeLogRetentionSweepConstruct', () => {
  it('passes the deployment-scoped prefix and configured retention to the handler', () => {
    synthConstruct(14).hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'handler.handler',
      Environment: {
        Variables: { LOG_GROUP_PREFIX: PREFIX, RETENTION_IN_DAYS: '14' },
      },
    });
  });

  it('scopes PutRetentionPolicy to this runtime\'s groups only', () => {
    const statements = statementsWithAction(synthConstruct(), 'logs:PutRetentionPolicy');
    expect(statements).toHaveLength(1);
    const resource = JSON.stringify(statements[0].Resource);
    expect(resource).toContain(`:log-group:${PREFIX}*`);
    expect(resource).not.toMatch(/:log-group:\*"/);
  });

  it('can never delete a log group or read log events', () => {
    const template = synthConstruct();
    for (const action of [
      'logs:DeleteLogGroup',
      'logs:DeleteRetentionPolicy',
      'logs:GetLogEvents',
      'logs:FilterLogEvents',
      'logs:StartQuery',
      'logs:*',
    ]) {
      expect(statementsWithAction(template, action)).toHaveLength(0);
    }
  });

  it('runs once a day', () => {
    synthConstruct().hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(1 day)',
      State: 'ENABLED',
      Targets: Match.arrayWith([Match.objectLike({ Arn: Match.anyValue() })]),
    });
  });
});

describe('PlatformStack wiring', () => {
  it('points the sweep at the stack\'s own runtime name', () => {
    const template = synthPlatform(true);
    const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
    expect(runtimes).toHaveLength(1);
    const runtimeName = (runtimes[0] as any).Properties.AgentRuntimeName;

    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          LOG_GROUP_PREFIX: `/aws/bedrock-agentcore/runtimes/${runtimeName}-`,
        }),
      },
    });
  });

  it('tolerates a runtime log group that does not exist yet on create and update', () => {
    // PutRetentionPolicy does not create the group. If the Runtime's first
    // start has not created it, the call must not fail the stack update.
    const template = synthPlatform(true);
    const retention = Object.values(template.findResources('Custom::AWS')).filter((res: any) =>
      JSON.stringify(res.Properties.Create ?? '').includes('putRetentionPolicy'));
    expect(retention).toHaveLength(1);

    const props = (retention[0] as any).Properties;
    for (const phase of ['Create', 'Update']) {
      // Serialized as Fn::Join when it embeds tokens such as the runtime id.
      const serialized = props[phase];
      const json = typeof serialized === 'string'
        ? serialized
        : serialized['Fn::Join'][1].map((part: unknown) =>
          (typeof part === 'string' ? part : 'TOKEN')).join('');
      const call = JSON.parse(json);
      expect(call.action).toBe('putRetentionPolicy');
      expect(call.ignoreErrorCodesMatching).toBe('ResourceNotFoundException');
    }
  });

  it('does not grant the retention custom resource CreateLogGroup', () => {
    const template = synthPlatform(true);
    const retentionStatements = statementsWithAction(template, 'logs:PutRetentionPolicy')
      .filter((s: any) => JSON.stringify(s.Resource).includes('-DEFAULT'));
    expect(retentionStatements).toHaveLength(1);
    expect([retentionStatements[0].Action].flat()).not.toContain('logs:CreateLogGroup');
  });

  it('creates neither the function nor the schedule when the kill switch is off', () => {
    const template = synthPlatform(false);
    const sweepFunctions = Object.values(template.findResources('AWS::Lambda::Function'))
      .filter((fn: any) => fn.Properties.Environment?.Variables?.LOG_GROUP_PREFIX);
    expect(sweepFunctions).toHaveLength(0);
    const rules = Object.values(template.findResources('AWS::Events::Rule'))
      .filter((rule: any) => String(rule.Properties.Name ?? '').includes('runtime-log-retention-sweep'));
    expect(rules).toHaveLength(0);
  });
});
