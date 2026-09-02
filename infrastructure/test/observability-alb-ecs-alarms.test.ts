import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

/**
 * Synthesized from the real PlatformStack, because what matters here is that the
 * alarms bound to the actual load balancer, target group, cluster and service.
 */
describe('ALB and ECS service alarms', () => {
  let template: Template;
  let alarms: Record<string, any>;

  /** Find an alarm by its unprefixed name. */
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

  describe('ALB alarms', () => {
    it('creates the six front-door alarms', () => {
      for (const name of [
        'alb-elb-5xx',
        'alb-target-5xx',
        'alb-unhealthy-hosts',
        'alb-target-connection-errors',
        'alb-rejected-connections',
        'alb-target-p99-latency',
      ]) {
        byName(name);
      }
    });

    // An undimensioned ALB alarm silently watches every load balancer in the
    // account: it deploys, evaluates, and never means what was intended.
    it('target alarms carry BOTH LoadBalancer and TargetGroup dimensions', () => {
      for (const name of ['alb-target-5xx', 'alb-unhealthy-hosts', 'alb-target-p99-latency']) {
        const dims = byName(name).Properties.Dimensions;
        const keys = dims.map((d: any) => d.Name).sort();
        expect(keys).toEqual(['LoadBalancer', 'TargetGroup']);
        // Values are CFN references to the real resources, not literals.
        for (const d of dims) {
          expect(JSON.stringify(d.Value)).toMatch(/Fn::GetAtt|Ref/);
        }
      }
    });

    it('load-balancer-scoped alarms carry the LoadBalancer dimension', () => {
      for (const name of ['alb-elb-5xx', 'alb-rejected-connections', 'alb-target-connection-errors']) {
        const dims = byName(name).Properties.Dimensions;
        expect(dims.map((d: any) => d.Name)).toContain('LoadBalancer');
      }
    });

    it('alarms on the right ALB metrics in the AWS/ApplicationELB namespace', () => {
      const expected: Record<string, string> = {
        'alb-elb-5xx': 'HTTPCode_ELB_5XX_Count',
        'alb-target-5xx': 'HTTPCode_Target_5XX_Count',
        'alb-unhealthy-hosts': 'UnHealthyHostCount',
        'alb-target-connection-errors': 'TargetConnectionErrorCount',
        'alb-rejected-connections': 'RejectedConnectionCount',
        'alb-target-p99-latency': 'TargetResponseTime',
      };
      for (const [name, metricName] of Object.entries(expected)) {
        const alarm = byName(name);
        expect(alarm.Properties.MetricName).toBe(metricName);
        expect(alarm.Properties.Namespace).toBe('AWS/ApplicationELB');
      }
    });

    // The metric stops arriving when no targets are registered, so absent data
    // is the outage. NOT_BREACHING would leave this silent during one.
    it('unhealthy-host alarm treats missing data as BREACHING', () => {
      expect(byName('alb-unhealthy-hosts').Properties.TreatMissingData).toBe('breaching');
    });

    it('error-count alarms treat missing data as NOT_BREACHING (no traffic is fine)', () => {
      for (const name of ['alb-elb-5xx', 'alb-target-5xx', 'alb-rejected-connections']) {
        expect(byName(name).Properties.TreatMissingData).toBe('notBreaching');
      }
    });

    // CloudWatch reports this metric in seconds, config is in ms.
    it('latency threshold is converted from config ms to CloudWatch seconds', () => {
      const alarm = byName('alb-target-p99-latency');
      // Default albP99LatencyMs is 120000 ms -> 120 s.
      expect(alarm.Properties.Threshold).toBe(120);
      expect(alarm.Properties.ExtendedStatistic).toBe('p99');
      // Comfortably above a normal streaming turn.
      expect(alarm.Properties.Threshold).toBeGreaterThan(30);
    });
  });

  describe('ECS service alarms', () => {
    it('creates the three service alarms', () => {
      for (const name of ['app-api-cpu-high', 'app-api-memory-high', 'app-api-running-tasks-low']) {
        byName(name);
      }
    });

    it('CPU and memory alarms carry BOTH ClusterName and ServiceName', () => {
      for (const name of ['app-api-cpu-high', 'app-api-memory-high']) {
        const dims = byName(name).Properties.Dimensions;
        const keys = dims.map((d: any) => d.Name).sort();
        expect(keys).toEqual(['ClusterName', 'ServiceName']);
      }
    });

    it('CPU and memory alarms use the AWS/ECS namespace and configured thresholds', () => {
      const cpu = byName('app-api-cpu-high');
      expect(cpu.Properties.Namespace).toBe('AWS/ECS');
      expect(cpu.Properties.MetricName).toBe('CPUUtilization');
      expect(cpu.Properties.Threshold).toBe(80);

      const mem = byName('app-api-memory-high');
      expect(mem.Properties.Namespace).toBe('AWS/ECS');
      expect(mem.Properties.MetricName).toBe('MemoryUtilization');
      expect(mem.Properties.Threshold).toBe(85);
    });

    it('running-task alarm reads Container Insights with both dimensions', () => {
      const alarm = byName('app-api-running-tasks-low');
      expect(alarm.Properties.Namespace).toBe('ECS/ContainerInsights');
      expect(alarm.Properties.MetricName).toBe('RunningTaskCount');
      const keys = alarm.Properties.Dimensions.map((d: any) => d.Name).sort();
      expect(keys).toEqual(['ClusterName', 'ServiceName']);
    });

    it('running-task alarm fires below desired count and treats missing data as BREACHING', () => {
      const alarm = byName('app-api-running-tasks-low');
      expect(alarm.Properties.ComparisonOperator).toBe('LessThanThreshold');
      expect(alarm.Properties.Threshold).toBe(1); // mock config desiredCount
      expect(alarm.Properties.TreatMissingData).toBe('breaching');
    });
  });

  it('all new alarms are routed to the alarm topic', () => {
    for (const name of [
      'alb-elb-5xx', 'alb-target-5xx', 'alb-unhealthy-hosts',
      'alb-target-connection-errors', 'alb-rejected-connections',
      'alb-target-p99-latency', 'app-api-cpu-high', 'app-api-memory-high',
      'app-api-running-tasks-low',
    ]) {
      const alarm = byName(name);
      expect(alarm.Properties.AlarmActions).toHaveLength(1);
      expect(alarm.Properties.OKActions).toHaveLength(1);
    }
  });
});
