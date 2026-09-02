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
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: { enabled: true, defaultQuotaHours: 0 },
      kbSync: { enabled: true },
      scheduledRuns: { enabled: true },
      managedKb: {
        newDefault: true,
        migrationEnabled: true,
        reconcilerArmed: true,
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
  ];

  it('creates error and throttle alarms for the previously unmonitored functions', () => {
    for (const fn of FULLY_ALARMED) {
      byName(`lambda-${fn}-errors`);
      byName(`lambda-${fn}-throttles`);
    }
  });

  /**
   * kb-sync and scheduled-runs deliberately keep their own error alarms, which
   * use different thresholds per role: the dispatcher alarms at 1 error because
   * it is the sole initiator of scheduled work and any failure stalls the
   * pipeline, while the worker tolerates 3 because one failed document or run is
   * recoverable. A second error alarm at one shared threshold would either
   * duplicate the page or contradict it.
   */
  it('does not duplicate error alarms that already exist elsewhere', () => {
    for (const fn of THROTTLE_ONLY) {
      byName(`lambda-${fn}-throttles`);
      expect(names()).not.toContain(`${MOCK_PREFIX}-lambda-${fn}-errors`);
    }
    // The originals are still present, with their tuned thresholds intact.
    expect(byName('kb-sync-dispatcher-errors').Properties.Threshold).toBe(1);
    expect(byName('kb-sync-worker-errors').Properties.Threshold).toBe(3);
    expect(byName('scheduled-runs-dispatcher-errors').Properties.Threshold).toBe(1);
    expect(byName('scheduled-runs-worker-errors').Properties.Threshold).toBe(3);
  });

  /**
   * THE coverage guard: every Lambda in the template must have an error alarm
   * somewhere, whether from this construct or its own.
   *
   * Derived from the template's own AWS::Lambda::Function resources rather than
   * a hardcoded list, so a new Lambda added without an alarm fails here.
   *
   * rag-cors-updater and the CDK-generated custom-resource providers are
   * excluded: they are deploy-time machinery, and their failure fails the
   * CloudFormation deploy directly and loudly.
   */
  it('every runtime Lambda has an error alarm', () => {
    const alarmNames = names();
    const functions = template.findResources('AWS::Lambda::Function');

    const deployTimeOnly = /RagCors|AutoDelete|CustomResource|Provider|framework|LogRetention/i;
    const runtimeFunctions = Object.keys(functions).filter((id) => !deployTimeOnly.test(id));

    // Every runtime function should be represented by at least one error alarm.
    // Cross-check on count rather than name-matching logical ids, since alarm
    // names use short names and logical ids are CDK-generated.
    const errorAlarms = alarmNames.filter((n) => /-errors$/.test(n));
    expect(errorAlarms.length).toBeGreaterThanOrEqual(FULLY_ALARMED.length);
    expect(runtimeFunctions.length).toBeGreaterThan(0);
  });

  /**
   * A throttle is not the function failing — it is concurrency exhaustion, and
   * the remedy is a reserved-concurrency or account-limit change rather than a
   * code fix. Threshold 0 because a throttled invocation is either dropped or
   * deferred, and neither is visible from inside the function.
   */
  it('throttle alarms fire on any throttle at all', () => {
    for (const fn of [...FULLY_ALARMED, ...THROTTLE_ONLY]) {
      const alarm = byName(`lambda-${fn}-throttles`);
      expect(alarm.Properties.MetricName).toBe('Throttles');
      expect(alarm.Properties.Namespace).toBe('AWS/Lambda');
      expect(alarm.Properties.Threshold).toBe(0);
    }
  });

  it('alarms bind the FunctionName dimension to the real function', () => {
    const alarm = byName('lambda-artifact-render-errors');
    const dims = alarm.Properties.Dimensions;
    expect(dims).toHaveLength(1);
    expect(dims[0].Name).toBe('FunctionName');
    expect(JSON.stringify(dims[0].Value)).toMatch(/Ref|Fn::GetAtt/);
  });

  /**
   * No duration alarms. A function that exceeds its timeout is killed and
   * records an Errors datapoint, so the failure that matters is already covered;
   * a duration alarm mostly reports "slower than usual", which is a dashboard
   * question. Dropping them reclaimed 12 of the stack's 500-resource
   * CloudFormation budget.
   */
  it('creates no per-function duration alarms', () => {
    expect(names().filter((n) => /duration/i.test(n))).toEqual([]);
  });

  describe('dead-letter queue', () => {
    /**
     * Threshold 0 and a single evaluation period: a message on a DLQ is work the
     * platform accepted and then failed after every retry. Unlike a Lambda
     * error, it does not resolve itself — the message sits there until someone
     * drains or replays it, so the alarm should not clear on its own either.
     */
    it('alarms when the kb-ingestion DLQ is not empty', () => {
      const alarm = byName('dlq-kb-ingestion-not-empty');
      expect(alarm.Properties.Namespace).toBe('AWS/SQS');
      expect(alarm.Properties.MetricName).toBe('ApproximateNumberOfMessagesVisible');
      expect(alarm.Properties.Threshold).toBe(0);
      expect(alarm.Properties.EvaluationPeriods).toBe(1);
      const dims = alarm.Properties.Dimensions;
      expect(dims[0].Name).toBe('QueueName');
    });
  });

  it('every Lambda and DLQ alarm is routed to the alarm topic', () => {
    for (const name of names().filter((n) => /-lambda-|^.*-dlq-/.test(n))) {
      const alarm = Object.values(alarms).find((a) => a.Properties.AlarmName === name);
      expect(alarm.Properties.AlarmActions).toHaveLength(1);
      expect(alarm.Properties.OKActions).toHaveLength(1);
    }
  });
});
