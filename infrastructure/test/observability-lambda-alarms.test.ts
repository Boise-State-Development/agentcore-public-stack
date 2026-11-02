import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

describe('Lambda and DLQ alarms', () => {
  let template: Template;
  let alarms: Record<string, any>;

  function names(): string[] {
    return Object.values(alarms)
      .map((a) => a.Properties.AlarmName as string)
      .filter((n) => typeof n === 'string');
  }

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
      kbSync: { enabled: true },
      scheduledRuns: { enabled: true },
      managedKb: {
        newDefault: true,
        migrationEnabled: true,
        reconcilerArmed: true,
        docReconcilerArmed: true,
        perOwnerDefaultBytes: 100 * 1024 * 1024,
        perOwnerElevatedBytes: 1024 * 1024 * 1024,
        perKnowledgeBaseCeilingBytes: 500 * 1024 * 1024,
        retentionWindowDays: 30,
        storageAlarmGb: 500,
        dailyCostAlarmUsd: 100,
      },
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

  /** Functions that get BOTH an error and a throttle alarm here. */
  const FULLY_ALARMED = [
    'artifact-render',
    'rag-ingestion',
    'kb-migration-dispatcher',
    'kb-migration-worker',
    'kb-migration-reconciler',
    'kb-ingestion-consumer',
    'conversation-index-consumer',
  ];

  /**
   * Functions whose own construct already defines an error alarm with a tuned
   * threshold, so only a throttle alarm is added here.
   */
  const THROTTLE_ONLY = [
    'kb-sync-dispatcher',
    'kb-sync-worker',
    'scheduled-runs-dispatcher',
    'scheduled-runs-worker',
    'memory-maintenance-worker',
  ];

  function groups(kind: 'errors' | 'throttles'): any[] {
    return Object.values(alarms).filter((a) => (
      typeof a.Properties.AlarmName === 'string'
      && a.Properties.AlarmName.startsWith(`${MOCK_PREFIX}-lambda-${kind}-group-`)
    ));
  }

  /** The grouped alarms whose description names `fn` as a member. */
  function groupsNaming(kind: 'errors' | 'throttles', fn: string): any[] {
    // Member lists are comma-separated, so match whole names: 'kb-sync-worker'
    // must not match inside 'kb-migration-worker'.
    const member = new RegExp(`(: |, )${fn}(, |\\. )`);
    return groups(kind).filter((a) => member.test(a.Properties.AlarmDescription));
  }

  it('puts every function in exactly one error group and one throttle group', () => {
    for (const fn of FULLY_ALARMED) {
      expect(groupsNaming('errors', fn)).toHaveLength(1);
      expect(groupsNaming('throttles', fn)).toHaveLength(1);
    }
  });

  // Those constructs keep their own error alarms with role-tuned thresholds
  // (dispatcher 1, worker 3); a second at one shared threshold would conflict.
  it('does not duplicate error alarms that already exist elsewhere', () => {
    for (const fn of THROTTLE_ONLY) {
      expect(groupsNaming('throttles', fn)).toHaveLength(1);
      expect(groupsNaming('errors', fn)).toHaveLength(0);
    }
    // The originals are still present, with their tuned thresholds intact.
    expect(byName('kb-sync-dispatcher-errors').Properties.Threshold).toBe(1);
    expect(byName('kb-sync-worker-errors').Properties.Threshold).toBe(3);
    expect(byName('scheduled-runs-dispatcher-errors').Properties.Threshold).toBe(1);
    expect(byName('scheduled-runs-worker-errors').Properties.Threshold).toBe(3);
    expect(byName('memory-maintenance-worker-errors').Properties.Threshold).toBe(1);
  });

  // One Errors (or Throttles) metric per member, so the member count in the
  // description and the metric count must agree.
  it('each group carries one metric per member, inside the 10-metric limit', () => {
    for (const kind of ['errors', 'throttles'] as const) {
      for (const alarm of groups(kind)) {
        const stats = alarm.Properties.Metrics.filter((m: any) => m.MetricStat);
        expect(stats.length).toBeLessThanOrEqual(10);
        const expected = kind === 'errors' ? 'Errors' : 'Throttles';
        for (const m of stats) {
          expect(m.MetricStat.Metric.Namespace).toBe('AWS/Lambda');
          expect(m.MetricStat.Metric.MetricName).toBe(expected);
        }
        const members = alarm.Properties.AlarmDescription.split(': ')[1].split('.')[0].split(', ');
        expect(members).toHaveLength(stats.length);
      }
    }
  });

  // MAX keeps the threshold per function rather than per group.
  it('alarms on the worst function in the group', () => {
    for (const alarm of [...groups('errors'), ...groups('throttles')]) {
      const expression = alarm.Properties.Metrics.find((m: any) => m.Expression).Expression;
      expect(expression).toMatch(/^MAX\(\[m0(, m\d+)*\]\)$/);
    }
  });

  it('throttle groups fire on any throttle at all', () => {
    for (const alarm of groups('throttles')) {
      expect(alarm.Properties.Threshold).toBe(0);
    }
  });

  it('alarms bind the FunctionName dimension to the real function', () => {
    const functionIds = new Set(Object.keys(template.findResources('AWS::Lambda::Function')));
    for (const alarm of [...groups('errors'), ...groups('throttles')]) {
      for (const m of alarm.Properties.Metrics.filter((x: any) => x.MetricStat)) {
        const dims = m.MetricStat.Metric.Dimensions;
        expect(dims).toHaveLength(1);
        expect(dims[0].Name).toBe('FunctionName');
        expect(functionIds.has(dims[0].Value.Ref)).toBe(true);
      }
    }
  });

  // A timed-out invocation already records an Errors datapoint.
  it('creates no per-function duration alarms', () => {
    expect(names().filter((n) => /duration/i.test(n))).toEqual([]);
  });

  describe('dead-letter queue', () => {
    // DLQ messages persist until drained or replayed, so this must not
    // self-clear.
    it('alarms when the kb-ingestion DLQ is not empty', () => {
      const alarm = byName('dlq-kb-ingestion-not-empty');
      expect(alarm.Properties.Namespace).toBe('AWS/SQS');
      expect(alarm.Properties.MetricName).toBe('ApproximateNumberOfMessagesVisible');
      expect(alarm.Properties.Threshold).toBe(0);
      expect(alarm.Properties.EvaluationPeriods).toBe(1);
      const dims = alarm.Properties.Dimensions;
      expect(dims[0].Name).toBe('QueueName');
    });

    it('alarms when the conversation-index DLQ is not empty', () => {
      const alarm = byName('dlq-conversation-index-not-empty');
      expect(alarm.Properties.Namespace).toBe('AWS/SQS');
      expect(alarm.Properties.Threshold).toBe(0);
    });
  });

  it('every Lambda and DLQ alarm is routed to the alarm topic', () => {
    for (const name of names().filter((n) => n.startsWith(`${MOCK_PREFIX}-lambda-`) || n.startsWith(`${MOCK_PREFIX}-dlq-`))) {
      const alarm = Object.values(alarms).find((a) => a.Properties.AlarmName === name);
      expect(alarm.Properties.AlarmActions).toHaveLength(1);
      expect(alarm.Properties.OKActions).toHaveLength(1);
    }
  });
});
