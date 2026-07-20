import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PromptCacheObservabilityConstruct } from '../lib/constructs/observability/prompt-cache-observability-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

function synth(production: boolean): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = createMockConfig({ production });
  new PromptCacheObservabilityConstruct(stack, 'PromptCacheObservability', {
    config,
  });
  return Template.fromStack(stack);
}

describe('PromptCacheObservabilityConstruct', () => {
  let t: Template;
  beforeAll(() => {
    t = synth(false);
  });

  it('creates the dashboard with the conventional name', () => {
    t.hasResourceProperties('AWS::CloudWatch::Dashboard', {
      DashboardName: `${MOCK_PREFIX}-prompt-cache-observability`,
    });
  });

  it('dashboard graphs the EMF namespace metrics and queries the runtime log group', () => {
    const dashboards = t.findResources('AWS::CloudWatch::Dashboard');
    const body = JSON.stringify(Object.values(dashboards)[0].Properties.DashboardBody);
    for (const metric of ['CacheReadTokens', 'CacheWriteTokens', 'AvoidableMiss', 'WastedUsd']) {
      expect(body).toContain(metric);
    }
    expect(body).toContain('AgentCoreStack/PromptCache');
    expect(body).toContain(`/aws/bedrock-agentcore/runtimes/${MOCK_PREFIX}`);
    expect(body).toContain('cacheStatus');
  });

  it('creates exactly two alarms, both NOT_BREACHING on missing data (kill-switch tolerant)', () => {
    t.resourceCountIs('AWS::CloudWatch::Alarm', 2);
    const alarms = Object.values(t.findResources('AWS::CloudWatch::Alarm'));
    for (const alarm of alarms) {
      expect(alarm.Properties.TreatMissingData).toBe('notBreaching');
      expect(alarm.Properties.Namespace).toBe('AgentCoreStack/PromptCache');
      expect(alarm.Properties.Statistic).toBe('Sum');
      // Console-only: no SNS wiring yet anywhere in the stack.
      expect(alarm.Properties.AlarmActions).toBeUndefined();
    }
  });

  it('alarms on AvoidableMiss (the nominated target) and WastedUsd', () => {
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-avoidable-miss`,
      MetricName: 'AvoidableMiss',
      Threshold: 50,
      EvaluationPeriods: 3,
      ComparisonOperator: 'GreaterThanThreshold',
    });
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-wasted-usd`,
      MetricName: 'WastedUsd',
      Threshold: 5,
    });
  });

  it('uses stricter thresholds in production', () => {
    const prod = synth(true);
    prod.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'AvoidableMiss',
      Threshold: 10,
    });
    prod.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'WastedUsd',
      Threshold: 1,
    });
  });

  it('exports the dashboard name', () => {
    t.hasOutput('*', {
      Export: { Name: `${MOCK_PREFIX}-PromptCacheDashboard` },
    });
  });
});
