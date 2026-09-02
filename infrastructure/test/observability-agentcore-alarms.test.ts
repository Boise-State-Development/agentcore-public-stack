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
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
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

  const AGENTCORE_ALARMS = [
    'agentcore-system-errors',
    'agentcore-high-error-rate',
    'agentcore-throttles',
    'agentcore-high-latency',
  ];

  it('creates the four runtime alarms', () => {
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
  it('binds the three-dimension runtime set on every alarm', () => {
    for (const name of AGENTCORE_ALARMS) {
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

  it('all four alarms are routed to the alarm topic', () => {
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
