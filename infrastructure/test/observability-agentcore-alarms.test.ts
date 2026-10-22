import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

/**
 * Pins the AgentCore metric binding, which was previously wrong: namespace
 * `bedrock-agentcore` with InvocationCount / InvocationErrors /
 * InvocationLatency, none of which exist. Both alarms had been in
 * INSUFFICIENT_DATA since creation, reading as healthy.
 */
describe('AgentCore Runtime alarms — verified metric binding', () => {
  const NAMESPACE = 'AWS/Bedrock-AgentCore';
  let template: Template;
  let alarms: Record<string, any>;

  function byName(name: string): any {
    const full = `${MOCK_PREFIX}-${name}`;
    const found = Object.values(alarms).find((a) => a.Properties.AlarmName === full);
    expect(found).toBeDefined();
    return found;
  }

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: {
        shareInboxEnabled: false, retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: { enabled: true, defaultQuotaHours: 0 },
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
    alarms = template.findResources('AWS::CloudWatch::Alarm');
  });

  /** Alarms on the per-invocation metric streams, dimensioned Resource/Operation/Name. */
  const INVOCATION_ALARMS = [
    'agentcore-system-errors',
    'agentcore-high-error-rate',
    'agentcore-throttles',
    'agentcore-high-latency',
  ];

  /**
   * `ActiveSessionCount` is an account-level gauge published with only a
   * `Service` dimension, so it is deliberately not in INVOCATION_ALARMS — the
   * three-dimension assertion below would fail it, correctly.
   */
  const ACCOUNT_GAUGE_ALARMS = ['agentcore-runtime-active-sessions'];

  const AGENTCORE_ALARMS = [...INVOCATION_ALARMS, ...ACCOUNT_GAUGE_ALARMS];

  it('creates the five runtime alarms', () => {
    for (const name of AGENTCORE_ALARMS) byName(name);
  });

  it('uses the AWS/Bedrock-AgentCore namespace', () => {
    for (const name of AGENTCORE_ALARMS) {
      expect(byName(name).Properties.Namespace).toBe(NAMESPACE);
    }
  });

  // Whole-template, so a dashboard widget cannot reintroduce them either.
  it('no alarm or dashboard references the non-existent metric names', () => {
    const whole = JSON.stringify(template.toJSON());
    for (const dead of ['InvocationCount', 'InvocationErrors', 'InvocationLatency']) {
      expect(whole).not.toContain(dead);
    }
    // The bare lowercase namespace must not appear as a metric namespace. It is
    // a real namespace, but it holds OTEL application metrics, not these.
    for (const alarm of Object.values(alarms)) {
      expect((alarm as any).Properties.Namespace).not.toBe('bedrock-agentcore');
    }
  });

  it('alarms on the verified metric names', () => {
    expect(byName('agentcore-system-errors').Properties.MetricName).toBe('SystemErrors');
    expect(byName('agentcore-high-error-rate').Properties.MetricName).toBe('UserErrors');
    expect(byName('agentcore-throttles').Properties.MetricName).toBe('Throttles');
    expect(byName('agentcore-high-latency').Properties.MetricName).toBe('Latency');
  });

  // Every stream here is dimensioned; an undimensioned metric matches nothing.
  it('binds the three-dimension runtime set on every invocation alarm', () => {
    for (const name of INVOCATION_ALARMS) {
      const dims = byName(name).Properties.Dimensions;
      const keys = dims.map((d: any) => d.Name).sort();
      expect(keys).toEqual(['Name', 'Operation', 'Resource']);

      const operation = dims.find((d: any) => d.Name === 'Operation');
      expect(operation.Value).toBe('InvokeAgentRuntime');

      // Resource is the runtime ARN, resolved from the real resource.
      const resource = dims.find((d: any) => d.Name === 'Resource');
      expect(JSON.stringify(resource.Value)).toMatch(/Fn::GetAtt|Ref/);

      // Name is {runtimeName}::DEFAULT, matching the endpoint qualifier used
      // for the runtime's log group.
      const nameDim = dims.find((d: any) => d.Name === 'Name');
      expect(JSON.stringify(nameDim.Value)).toContain('::DEFAULT');
    }
  });

  it('does not bind the ComputeType implementation detail', () => {
    for (const name of AGENTCORE_ALARMS) {
      const keys = byName(name).Properties.Dimensions.map((d: any) => d.Name);
      expect(keys).not.toContain('ComputeType');
    }
  });

  // Milliseconds, unlike the ALB metric. Measured turns peak near 25s, so the
  // previous 30000 threshold sat just above normal.
  it('latency threshold is in milliseconds and clears a real long turn', () => {
    const alarm = byName('agentcore-high-latency');
    expect(alarm.Properties.Threshold).toBe(120_000);
    expect(alarm.Properties.ExtendedStatistic).toBe('p99');
    expect(alarm.Properties.Threshold).toBeGreaterThan(24_400);
  });

  it('separates system errors from user errors', () => {
    expect(byName('agentcore-system-errors').Properties.MetricName)
      .not.toBe(byName('agentcore-high-error-rate').Properties.MetricName);
  });

  it('throttle alarm fires on any throttle at all', () => {
    expect(byName('agentcore-throttles').Properties.Threshold).toBe(0);
  });

  // Mirrors `agentcore-code-interpreter-active-sessions`, which watches the same
  // metric on the CodeInterpreter Service dimension.
  describe('concurrent runtime sessions', () => {
    it('alarms on ActiveSessionCount for the Runtime service dimension', () => {
      const alarm = byName('agentcore-runtime-active-sessions');
      expect(alarm.Properties.MetricName).toBe('ActiveSessionCount');
      const service = alarm.Properties.Dimensions.find((d: any) => d.Name === 'Service');
      expect(service).toBeDefined();
      expect(service.Value).toBe('AgentCore.Runtime');
    });

    // An account-level gauge carries no Resource: adding one would match no
    // stream and the alarm would sit in INSUFFICIENT_DATA, reading as healthy.
    it('carries only the Service dimension', () => {
      const dims = byName('agentcore-runtime-active-sessions').Properties.Dimensions;
      expect(dims.map((d: any) => d.Name)).toEqual(['Service']);
    });

    it('reads the peak of the period, sustained over a full hour', () => {
      const alarm = byName('agentcore-runtime-active-sessions');
      // Maximum, not Average: a gauge averaged over 5 minutes hides the peak
      // that matters.
      expect(alarm.Properties.Statistic).toBe('Maximum');
      expect(alarm.Properties.Period).toBe(300);
      // 12 x 5 min = 1 hour sustained. The window, not the threshold, is what
      // distinguishes a real accumulation regression from a load test: on 7 days
      // of prod data the previous 3-period (15 min) window fired on all three
      // nightly load tests, and no threshold separates them (see config.ts).
      expect(alarm.Properties.EvaluationPeriods).toBe(12);
      expect(alarm.Properties.Period * alarm.Properties.EvaluationPeriods).toBe(3_600);
      // Every datapoint must breach; a dip resets. Accumulation that reaps
      // itself is not the failure mode being watched.
      expect(alarm.Properties.DatapointsToAlarm ?? alarm.Properties.EvaluationPeriods).toBe(12);
      expect(alarm.Properties.ComparisonOperator).toBe('GreaterThanThreshold');
      expect(alarm.Properties.TreatMissingData).toBe('notBreaching');
    });

    /**
     * The #1016 lesson, pinned. That alarm sat above threshold for 195 of 197
     * datapoints because an account-wide roll-up was compared against a number
     * that did not denominate it. Here the threshold has to clear a normal day
     * by a wide margin — measured prod traffic outside load-test bursts is max
     * 8-16 with a mean of 2.4-6.8, and dev peaks at 5 — while staying far below
     * the 5,000 concurrent-session account quota, which `agentcore-throttles`
     * owns.
     *
     * The upper bound also guards the other direction: a threshold high enough
     * to sit above a load test (~1500) would sit above the ~99-sustained #338
     * regime too, and the alarm would never fire on the regression it exists
     * for. Duration, not magnitude, does that separation — see the window test
     * above.
     */
    it('is thresholded well above normal traffic and well below the account quota', () => {
      const threshold = byName('agentcore-runtime-active-sessions').Properties.Threshold;
      expect(threshold).toBe(
        createMockConfig({}).observability.agentCoreActiveSessionThreshold,
      );
      // Comfortably above a normal day (peaks in the tens)...
      expect(threshold).toBeGreaterThan(20);
      // ...and below the ~99 sustained by the #338 regression, so that regime
      // actually trips it.
      expect(threshold).toBeLessThan(99);
      expect(threshold).toBeLessThan(5_000 * 0.1);
    });
  });

  it('all five alarms are routed to the alarm topic', () => {
    for (const name of AGENTCORE_ALARMS) {
      expect(byName(name).Properties.AlarmActions).toHaveLength(1);
      expect(byName(name).Properties.OKActions).toHaveLength(1);
    }
  });

  describe('dashboard', () => {
    it('graphs the verified namespace and metrics', () => {
      const dashboards = template.findResources('AWS::CloudWatch::Dashboard');
      const agentcore = Object.values(dashboards).find((d: any) =>
        JSON.stringify(d.Properties.DashboardName).includes('agentcore-observability'),
      );
      expect(agentcore).toBeDefined();
      const body = JSON.stringify((agentcore as any).Properties.DashboardBody);

      expect(body).toContain(NAMESPACE);
      for (const metric of [
        'Invocations', 'SystemErrors', 'UserErrors', 'Throttles',
        'Sessions', 'Latency', 'ActiveSessionCount',
      ]) {
        expect(body).toContain(metric);
      }
    });

    // InputTokens/OutputTokens do not exist; the token metrics in this namespace
    // are Memory-strategy counters, not model tokens.
    it('does not graph non-existent token metrics, and points at the right dashboard', () => {
      const dashboards = template.findResources('AWS::CloudWatch::Dashboard');
      const agentcore = Object.values(dashboards).find((d: any) =>
        JSON.stringify(d.Properties.DashboardName).includes('agentcore-observability'),
      );
      const body = JSON.stringify((agentcore as any).Properties.DashboardBody);
      expect(body).not.toContain('InputTokens"');
      expect(body).not.toContain('OutputTokens"');
      expect(body).toContain('prompt-cache-observability');
    });
  });
});
