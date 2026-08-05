import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PromptCacheObservabilityConstruct } from '../lib/constructs/observability/prompt-cache-observability-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

// The shape the AgentCore service actually uses: runtime *id* (with its
// AWS-assigned suffix) + endpoint qualifier. Deliberately NOT the project
// prefix — querying that name is the bug this fixture guards against.
const MOCK_RUNTIME_LOG_GROUP =
  `/aws/bedrock-agentcore/runtimes/${MOCK_PREFIX}_agentcore_runtime-AbC123XyZ0-DEFAULT`;

function synth(production: boolean): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = createMockConfig({ production });
  new PromptCacheObservabilityConstruct(stack, 'PromptCacheObservability', {
    config,
    runtimeLogGroupName: MOCK_RUNTIME_LOG_GROUP,
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
    for (const metric of [
      'CacheReadTokens',
      'CacheWriteTokens',
      'AvoidableMiss',
      'PartialMiss',
      'PartialMissUsd',
      'WastedUsd',
      'SessionPartialMissUsd',
    ]) {
      expect(body).toContain(metric);
    }
    expect(body).toContain('AgentCoreStack/PromptCache');
    // Must be the runtime's own group, not a prefix-named one — the latter
    // exists in no account and silently returns zero rows.
    expect(body).toContain(MOCK_RUNTIME_LOG_GROUP);
    expect(body).toContain('cacheStatus');
    // The alarm can only say a session crossed the line; this widget says which.
    expect(body).toContain('sessionId');
  });

  it('creates three alarms, all NOT_BREACHING on missing data (kill-switch tolerant)', () => {
    t.resourceCountIs('AWS::CloudWatch::Alarm', 3);
    const alarms = Object.values(t.findResources('AWS::CloudWatch::Alarm'));
    for (const alarm of alarms) {
      expect(alarm.Properties.TreatMissingData).toBe('notBreaching');
      expect(alarm.Properties.Namespace).toBe('AgentCoreStack/PromptCache');
      // Console-only: no SNS wiring yet anywhere in the stack.
      expect(alarm.Properties.AlarmActions).toBeUndefined();
    }
  });

  it('alarms on AvoidableMiss (the nominated target) and WastedUsd', () => {
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-avoidable-miss`,
      MetricName: 'AvoidableMiss',
      Statistic: 'Sum',
      Threshold: 50,
      EvaluationPeriods: 3,
      ComparisonOperator: 'GreaterThanThreshold',
    });
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-wasted-usd`,
      MetricName: 'WastedUsd',
      Statistic: 'Sum',
      Threshold: 5,
    });
  });

  it('alarms on one session accumulating $5 of partial-miss waste', () => {
    // The fleet sums above cannot see a single conversation spending $0.43 a
    // turn for five days — this is the alarm that would have caught the
    // motivating incident, on its second day.
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-session-partial-miss`,
      MetricName: 'SessionPartialMissUsd',
      // Cumulative per session, so Maximum (not Sum) answers "is any single
      // session over the line", and the period is the 24h the spec asks for.
      Statistic: 'Maximum',
      Period: 86400,
      Threshold: 5,
      EvaluationPeriods: 1,
      ComparisonOperator: 'GreaterThanThreshold',
    });
  });

  it('holds the session threshold at $5 in production too', () => {
    // Unlike the fleet alarms, this one is not traffic-scaled: $5 of waste in
    // one conversation is the same problem in dev and in prod.
    synth(true).hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'SessionPartialMissUsd',
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
