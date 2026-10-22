import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

describe('AI-path alarms (Bedrock, Memory, Gateway, Code Interpreter)', () => {
  const AGENTCORE_NS = 'AWS/Bedrock-AgentCore';
  let template: Template;
  let alarms: Record<string, any>;

  function byName(name: string): any {
    const full = `${MOCK_PREFIX}-${name}`;
    const found = Object.values(alarms).find((a) => a.Properties.AlarmName === full);
    expect(found).toBeDefined();
    return found;
  }

  function allNames(): string[] {
    return Object.values(alarms)
      .map((a) => a.Properties.AlarmName as string)
      .filter((n) => typeof n === 'string');
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

  describe('Bedrock inference', () => {
    // The two quota alarms mock-config configures, and their 75%-of-quota
    // thresholds. Kept next to the assertions so a mock change that silently
    // drops a model fails here rather than reducing coverage unnoticed.
    const SONNET_5 = 'bedrock-tpm-quota-usage-global-anthropic-claude-sonnet-5';
    const SONNET_4 = 'bedrock-tpm-quota-usage-us-anthropic-claude-sonnet-4-20250514-v1-0';

    it('alarms on throttles and server errors', () => {
      for (const name of [
        'bedrock-invocation-throttles',
        'bedrock-invocation-server-errors',
      ]) {
        expect(byName(name).Properties.Namespace).toBe('AWS/Bedrock');
      }
    });

    it('uses the account-wide roll-up for throttles, which need no denominator', () => {
      expect(byName('bedrock-invocation-throttles').Properties.Dimensions).toBeUndefined();
    });

    it('throttle alarm fires on any throttle', () => {
      const alarm = byName('bedrock-invocation-throttles');
      expect(alarm.Properties.MetricName).toBe('InvocationThrottles');
      expect(alarm.Properties.Threshold).toBe(0);
      // Zero streams when verified — never fired, rather than absent.
      expect(alarm.Properties.TreatMissingData).toBe('notBreaching');
    });

    // Regression guard. The original alarm compared this metric against 80 as if
    // it were a percentage; it is an absolute token count, so 80 was crossed by
    // roughly one sentence of output and the alarm sat in ALARM ~99% of the time,
    // clearing only when a period had no data at all.
    it('creates no account-wide quota alarm, which could not be a percentage', () => {
      // Sort both sides: alarm discovery order is not guaranteed.
      expect(allNames().filter((n) => /tpm-quota/.test(n)).sort())
        .toEqual([`${MOCK_PREFIX}-${SONNET_5}`, `${MOCK_PREFIX}-${SONNET_4}`].sort());
      for (const n of allNames()) {
        expect(n).not.toBe(`${MOCK_PREFIX}-bedrock-tpm-quota-usage`);
      }
    });

    it('quota alarms are per-model, scoped by the ModelId dimension', () => {
      for (const [name, modelId] of [
        [SONNET_5, 'global.anthropic.claude-sonnet-5'],
        [SONNET_4, 'us.anthropic.claude-sonnet-4-20250514-v1:0'],
      ] as const) {
        const alarm = byName(name);
        expect(alarm.Properties.MetricName).toBe('EstimatedTPMQuotaUsage');
        expect(alarm.Properties.Namespace).toBe('AWS/Bedrock');
        expect(alarm.Properties.Dimensions).toEqual([{ Name: 'ModelId', Value: modelId }]);
      }
    });

    // Maximum, not Average: the quota is per minute, the period is five, and the
    // underlying data is 1-minute. Averaging would dilute a real spike.
    it('quota alarm threshold is the configured percent of each model quota', () => {
      expect(byName(SONNET_5).Properties.Threshold).toBe(30_000_000); // 75% of 40M
      expect(byName(SONNET_4).Properties.Threshold).toBe(150_000); //    75% of 200k
      for (const name of [SONNET_5, SONNET_4]) {
        expect(byName(name).Properties.Statistic).toBe('Maximum');
        expect(byName(name).Properties.TreatMissingData).toBe('notBreaching');
      }
    });
  });

  describe('AgentCore Memory', () => {
    it('alarms on hot-path system errors and throttles', () => {
      for (const name of [
        'agentcore-memory-system-errors',
        'agentcore-memory-throttles',
      ]) {
        const alarm = byName(name);
        // Per-Operation streams mean these render as metric math.
        expect(Array.isArray(alarm.Properties.Metrics)).toBe(true);
        expect(JSON.stringify(alarm.Properties.Metrics)).toContain(AGENTCORE_NS);
      }
    });

    // Extraction and Consolidation are async and do not break a live turn.
    it('sums only the conversation hot-path operations', () => {
      const json = JSON.stringify(byName('agentcore-memory-system-errors').Properties.Metrics);
      for (const op of [
        'CreateEvent', 'RetrieveMemoryRecords', 'GetMemoryRecord', 'ListEvents', 'GetMemory',
      ]) {
        expect(json).toContain(op);
      }
      expect(json).not.toContain('Extraction');
      expect(json).not.toContain('Consolidation');
    });

    it('binds Resource to the Memory ARN', () => {
      const metrics = byName('agentcore-memory-system-errors').Properties.Metrics;
      const stat = metrics.find((m: any) => m.MetricStat);
      const resource = stat.MetricStat.Metric.Dimensions.find((d: any) => d.Name === 'Resource');
      // Memory publishes a full ARN in this dimension.
      expect(JSON.stringify(resource.Value)).toMatch(/Fn::GetAtt|Ref|arn:/);
    });

    it('stays inside the 10-metric math limit', () => {
      const metrics = byName('agentcore-memory-system-errors').Properties.Metrics;
      expect(metrics.filter((m: any) => m.MetricStat).length).toBe(5);
    });
  });

  describe('AgentCore Gateway', () => {
    it('alarms on MCP system errors and throttles with the method roll-up', () => {
      for (const name of [
        'agentcore-gateway-system-errors',
        'agentcore-gateway-throttles',
      ]) {
        const alarm = byName(name);
        expect(alarm.Properties.Namespace).toBe(AGENTCORE_NS);
        const keys = alarm.Properties.Dimensions.map((d: any) => d.Name).sort();
        // Resource + Operation + Protocol, and deliberately NOT Method: a
        // per-Method alarm set would multiply with every tool exposed.
        expect(keys).toEqual(['Operation', 'Protocol', 'Resource']);
        const protocol = alarm.Properties.Dimensions.find((d: any) => d.Name === 'Protocol');
        expect(protocol.Value).toBe('MCP');
      }
    });
  });

  describe('AgentCore Code Interpreter', () => {
    it('alarms on session system errors across all three operations', () => {
      const json = JSON.stringify(
        byName('agentcore-code-interpreter-system-errors').Properties.Metrics,
      );
      for (const op of [
        'StartCodeInterpreterSession', 'InvokeCodeInterpreter', 'StopCodeInterpreterSession',
      ]) {
        expect(json).toContain(op);
      }
    });

    // Memory and Gateway use full ARNs for this same key; an ARN here matches
    // no stream.
    it('binds Resource to the bare Code Interpreter ID, not an ARN', () => {
      const metrics = byName('agentcore-code-interpreter-system-errors').Properties.Metrics;
      const stat = metrics.find((m: any) => m.MetricStat);
      const resource = stat.MetricStat.Metric.Dimensions.find((d: any) => d.Name === 'Resource');
      expect(JSON.stringify(resource.Value)).not.toContain('arn:aws:bedrock-agentcore');
    });

    it('alarms on concurrent session count as an account-level gauge', () => {
      const alarm = byName('agentcore-code-interpreter-active-sessions');
      expect(alarm.Properties.MetricName).toBe('ActiveSessionCount');
      const service = alarm.Properties.Dimensions.find((d: any) => d.Name === 'Service');
      expect(service.Value).toBe('AgentCore.CodeInterpreter');
    });
  });

  describe('deliberate omissions', () => {
    // AWS/Cognito publishes only success metrics on the ESSENTIALS plan; failure
    // metrics need Plus. The auth signal is the token-enrichment Lambda.
    it('creates no Cognito alarm, because no failure metric exists to watch', () => {
      for (const alarm of Object.values(alarms)) {
        expect((alarm as any).Properties.Namespace).not.toBe('AWS/Cognito');
      }
      expect(allNames().filter((n) => /cognito|sign-in/i.test(n))).toEqual([]);
    });

    it('creates no AgentCore Browser alarm', () => {
      expect(allNames().filter((n) => /browser/i.test(n))).toEqual([]);
    });
  });

  it('all AI-path alarms are routed to the alarm topic', () => {
    for (const name of [
      'bedrock-invocation-throttles', 'bedrock-invocation-server-errors',
      'bedrock-tpm-quota-usage-global-anthropic-claude-sonnet-5',
      'bedrock-tpm-quota-usage-us-anthropic-claude-sonnet-4-20250514-v1-0',
      'agentcore-memory-system-errors',
      'agentcore-memory-throttles', 'agentcore-gateway-system-errors',
      'agentcore-gateway-throttles', 'agentcore-code-interpreter-system-errors',
      'agentcore-code-interpreter-active-sessions',
    ]) {
      const alarm = byName(name);
      expect(alarm.Properties.AlarmActions).toHaveLength(1);
      expect(alarm.Properties.OKActions).toHaveLength(1);
    }
  });
});
