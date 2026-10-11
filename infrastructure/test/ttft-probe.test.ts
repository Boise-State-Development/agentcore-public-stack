// Scheduled time-to-first-token probe (docs/specs/turn-path-ttft.md §6E).
//
// Pinned: the schedule runs the probe module on app-api's task definition
// family with the pruner's role (no new role or policy, the stack is near its
// resource limit); it is DISABLED with no alarm until a probe user is set; set,
// it is ENABLED, carries the user and model, and alarms when no complete run
// lands for three hours, treating missing data as breaching.
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AppConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { TTFT_PROBE_COMMAND, TTFT_PROBE_SCHEDULE } from '../lib/constructs/observability/ttft-probe-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

function synth(overrides: Partial<AppConfig> = {}): Template {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', { config, env: { account: MOCK_ACCOUNT, region: MOCK_REGION } });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function probeRule(t: Template): any {
  const rules = Object.values(t.findResources('AWS::Events::Rule', {
    Properties: { ScheduleExpression: TTFT_PROBE_SCHEDULE },
  }));
  expect(rules).toHaveLength(1);
  return rules[0];
}

describe('TTFT probe', () => {
  it('is disabled, with no alarm, until a probe user is configured', () => {
    const t = synth();
    expect(probeRule(t).Properties.State).toBe('DISABLED');
    t.resourcePropertiesCountIs('AWS::CloudWatch::Alarm', { AlarmName: `${MOCK_PREFIX}-ttft-probe-not-reporting` }, 0);
  });

  it('runs the probe module on the app-api task family with the pruner role', () => {
    const t = synth({ ttftProbe: { userId: 'probe-sub', modelId: 'us.anthropic.claude-haiku-4-5' } });
    const rule = probeRule(t);
    expect(rule.Properties.State).toBe('ENABLED');
    const target = rule.Properties.Targets[0];
    const input = JSON.parse(target.Input);
    expect(input.containerOverrides[0].command).toEqual(TTFT_PROBE_COMMAND);
    expect(input.containerOverrides[0].environment).toEqual([
      { name: 'TTFT_PROBE_USER_ID', value: 'probe-sub' },
      { name: 'TTFT_PROBE_MODEL_ID', value: 'us.anthropic.claude-haiku-4-5' },
    ]);
    expect(target.RetryPolicy.MaximumRetryAttempts).toBe(0);
    // Same role as the session retention pruner's schedule.
    const pruner = Object.values(t.findResources('AWS::Events::Rule')).find(
      (r: any) => r.Properties.Description?.startsWith('Session retention pruner'),
    ) as any;
    expect(target.RoleArn).toEqual(pruner.Properties.Targets[0].RoleArn);
  });

  it('omits the model override when none is pinned', () => {
    const t = synth({ ttftProbe: { userId: 'probe-sub', modelId: '' } });
    const input = JSON.parse(probeRule(t).Properties.Targets[0].Input);
    expect(input.containerOverrides[0].environment).toEqual([{ name: 'TTFT_PROBE_USER_ID', value: 'probe-sub' }]);
  });

  it('alarms when no complete run lands for three hours, missing data breaching', () => {
    const t = synth({ ttftProbe: { userId: 'probe-sub', modelId: '' } });
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-ttft-probe-not-reporting`,
      Namespace: 'AgentCoreStack/TurnLatency',
      MetricName: 'TtftProbeRuns',
      Statistic: 'Sum',
      Period: 3600,
      EvaluationPeriods: 3,
      Threshold: 1,
      ComparisonOperator: 'LessThanThreshold',
      TreatMissingData: 'breaching',
      AlarmActions: Match.anyValue(),
    });
  });

  it('adds no IAM role or policy of its own', () => {
    const off = synth();
    const on = synth({ ttftProbe: { userId: 'probe-sub', modelId: '' } });
    for (const type of ['AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
      expect(Object.keys(on.findResources(type)).length).toBe(Object.keys(off.findResources(type)).length);
    }
  });
});
