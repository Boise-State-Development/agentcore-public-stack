import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as sns from 'aws-cdk-lib/aws-sns';

import {
  AppConfig,
  OBSERVABILITY_DEFAULT_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD,
  OBSERVABILITY_DEFAULT_PROMPT_CACHE_WASTED_USD_THRESHOLD,
} from '../lib/config';

import { PromptCacheObservabilityConstruct } from '../lib/constructs/observability/prompt-cache-observability-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

// The shape the AgentCore service actually uses: runtime *id* (with its
// AWS-assigned suffix) + endpoint qualifier. Deliberately NOT the project
// prefix — querying that name is the bug this fixture guards against.
const MOCK_RUNTIME_LOG_GROUP =
  `/aws/bedrock-agentcore/runtimes/${MOCK_PREFIX}_agentcore_runtime-AbC123XyZ0-DEFAULT`;

/**
 * Synthesize the construct with a real SNS topic attached.
 *
 * There is no `production` parameter any more. Thresholds are single configured
 * values: this repo is forked by many institutions, so a fork with one
 * environment should not have to reason about a `production` boolean, and a fork
 * with three should not be limited to two. Per-environment differences live in
 * the forker's deployment config and arrive as one value.
 */
function synth(observabilityOverrides: Partial<AppConfig['observability']> = {}): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const base = createMockConfig();
  const config: AppConfig = {
    ...base,
    observability: { ...base.observability, ...observabilityOverrides },
  };
  const topic = new sns.Topic(stack, 'TestAlarmTopic');
  new PromptCacheObservabilityConstruct(stack, 'PromptCacheObservability', {
    config,
    runtimeLogGroupName: MOCK_RUNTIME_LOG_GROUP,
    alarmTopic: topic,
  });
  return Template.fromStack(stack);
}

describe('PromptCacheObservabilityConstruct', () => {
  let t: Template;
  beforeAll(() => {
    t = synth();
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
      // Routed to the platform alarm topic. This assertion previously read
      // `toBeUndefined()` with the note "Console-only: no SNS wiring yet
      // anywhere in the stack" — which was true of the whole stack, and is the
      // gap the alarm topic + AlarmFactory closed. A cost alarm nobody is told
      // about is the one kind that matters least in the console and most in an
      // inbox: the motivating incident leaked $27 over five days precisely
      // because no one was watching a screen.
      expect(alarm.Properties.AlarmActions).toHaveLength(1);
      expect(alarm.Properties.OKActions).toHaveLength(1);
    }
  });

  it('alarms on AvoidableMiss (the nominated target) and WastedUsd', () => {
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-avoidable-miss`,
      MetricName: 'AvoidableMiss',
      Statistic: 'Sum',
      // The single default. Was `config.production ? 10 : 50`; the tighter
      // value became the one default because a prefix-stability regression is
      // a cost leak, and catching it earlier is cheaper for every fork.
      Threshold: OBSERVABILITY_DEFAULT_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD,
      EvaluationPeriods: 3,
      ComparisonOperator: 'GreaterThanThreshold',
    });
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-prompt-cache-wasted-usd`,
      MetricName: 'WastedUsd',
      Statistic: 'Sum',
      Threshold: OBSERVABILITY_DEFAULT_PROMPT_CACHE_WASTED_USD_THRESHOLD,
    });
  });

  it('alarms on one session accumulating the configured partial-miss waste', () => {
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

  /**
   * Every threshold is a single configured value. An institution that wants a
   * looser dev environment sets a different value there — it does not get one
   * implicitly from a `production` flag it may never have set. That is the
   * whole point of the single-value rule for an OSS repo: the defaults have to
   * be right for a fork that configures nothing.
   */
  it('thresholds come from config, not from a production branch', () => {
    const custom = synth({
      promptCacheAvoidableMissThreshold: 77,
      promptCacheWastedUsdThreshold: 8.5,
      promptCacheSessionWastedUsdThreshold: 42,
    });
    custom.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'AvoidableMiss',
      Threshold: 77,
    });
    custom.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'WastedUsd',
      // Fractional dollars survive: parseFloatEnv, not parseIntEnv.
      Threshold: 8.5,
    });
    custom.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'SessionPartialMissUsd',
      Threshold: 42,
    });
  });

  /**
   * The opt-out path: no topic means alarms are still created, just
   * console-only. That was the stack's behaviour before the alarm topic
   * existed, kept reachable for a fork that routes alerts another way.
   */
  it('stays console-only when no alarm topic is supplied', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'NoTopic', {
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    new PromptCacheObservabilityConstruct(stack, 'PromptCacheObservability', {
      config: createMockConfig(),
      runtimeLogGroupName: MOCK_RUNTIME_LOG_GROUP,
    });
    const noTopic = Template.fromStack(stack);
    noTopic.resourceCountIs('AWS::CloudWatch::Alarm', 3);
    for (const alarm of Object.values(noTopic.findResources('AWS::CloudWatch::Alarm'))) {
      expect((alarm as any).Properties.AlarmActions).toBeUndefined();
    }
  });

  it('exports the dashboard name', () => {
    t.hasOutput('*', {
      Export: { Name: `${MOCK_PREFIX}-PromptCacheDashboard` },
    });
  });
});
