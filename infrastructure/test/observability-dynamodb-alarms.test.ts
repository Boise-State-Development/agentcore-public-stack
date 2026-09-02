import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

describe('DynamoDB per-table alarms', () => {
  let template: Template;
  let alarms: Record<string, any>;
  let tableCount: number;

  function ddbAlarmNames(): string[] {
    return Object.values(alarms)
      .map((a) => a.Properties.AlarmName as string)
      .filter((n) => typeof n === 'string' && n.startsWith(`${MOCK_PREFIX}-ddb-`));
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
    tableCount = Object.keys(template.findResources('AWS::DynamoDB::Table')).length;
  });

  /**
   * THE coverage guard.
   *
   * Ties alarm coverage to the actual table count in the template, so adding a
   * 27th table without adding it to the alarm list fails here rather than
   * shipping a silently unmonitored table. A hardcoded number would have to be
   * updated by the same person who forgot the table, which is no guard at all.
   */
  it('covers every table in the stack — one throttle alarm each', () => {
    expect(tableCount).toBe(26);
    // 26 per-table throttle alarms + 1 account-level UserErrors alarm.
    expect(ddbAlarmNames()).toHaveLength(tableCount + 1);
  });

  it('every table has a throttle alarm naming that table', () => {
    const names = new Set(ddbAlarmNames());
    // Derive expected table short-names from the template's OWN TableName
    // properties, so this does not restate the list the construct is given.
    const tableShortNames = Object.values(template.findResources('AWS::DynamoDB::Table'))
      .map((t: any) => (t.Properties.TableName as string).replace(`${MOCK_PREFIX}-`, ''));

    const missing = tableShortNames
      .map((short) => `${MOCK_PREFIX}-ddb-${short}-throttle`)
      .filter((expected) => !names.has(expected));
    expect(missing).toEqual([]);
  });

  /**
   * Read and write throttles share one alarm because the split cost 26 of the
   * stack's 500-resource CloudFormation budget for a signal with ZERO recorded
   * occurrences in the live account — every table is on-demand, so capacity
   * scales automatically and throttling has never happened. The expression sums
   * both metrics and the alarm description names them, so the read-vs-write
   * diagnosis is written down rather than lost.
   */
  it('throttle alarm sums read and write events in one expression', () => {
    const alarm = Object.values(alarms).find(
      (a) => a.Properties.AlarmName === `${MOCK_PREFIX}-ddb-users-throttle`,
    );
    expect(alarm).toBeDefined();
    // Metric math renders as Metrics[], not a flat MetricName.
    expect(alarm.Properties.MetricName).toBeUndefined();
    const json = JSON.stringify(alarm.Properties.Metrics);
    expect(json).toContain('ReadThrottleEvents');
    expect(json).toContain('WriteThrottleEvents');
    expect(json).toContain('reads + writes');
    expect(alarm.Properties.Threshold).toBe(10); // configured default
    expect(alarm.Properties.TreatMissingData).toBe('notBreaching');
  });

  /**
   * Two metrics, far inside CloudWatch's cap of 10 individual metrics per
   * math-expression alarm. That cap is not theoretical: CDK's
   * metricSystemErrorsForOperations() defaults to all 14 DynamoDB operations and
   * throws TooManyMetricsInMathExpression at synth.
   */
  it('throttle expression stays well inside the 10-metric math limit', () => {
    const alarm = Object.values(alarms).find(
      (a) => a.Properties.AlarmName === `${MOCK_PREFIX}-ddb-users-throttle`,
    );
    const metricCount = alarm.Properties.Metrics.filter((m: any) => m.MetricStat).length;
    expect(metricCount).toBe(2);
  });

  /**
   * The TableName dimension must come from the real resource. A dimension built
   * from a name string produces an alarm that looks correct and watches a table
   * that may not exist.
   */
  it('throttle alarms bind TableName to the real table resource', () => {
    const alarm = Object.values(alarms).find(
      (a) => a.Properties.AlarmName === `${MOCK_PREFIX}-ddb-sessions-metadata-throttle`,
    );
    const stats = alarm.Properties.Metrics.filter((m: any) => m.MetricStat);
    expect(stats.length).toBeGreaterThan(0);
    for (const m of stats) {
      const dims = m.MetricStat.Metric.Dimensions;
      expect(dims).toHaveLength(1);
      expect(dims[0].Name).toBe('TableName');
      expect(JSON.stringify(dims[0].Value)).toMatch(/Ref|Fn::GetAtt/);
    }
  });

  /**
   * Replaces the 26 per-table SystemErrors alarms.
   *
   * Verified against the live account: SystemErrors had ZERO metric streams (it
   * has never fired, and AWS-side 5xx affect more than one table anyway), while
   * account-level UserErrors had real data — 3 errors one day and 7 another in
   * the trailing fortnight. So 26 alarms on a signal that has never fired were
   * traded for 1 alarm on a signal that is firing today.
   */
  it('has one account-level UserErrors alarm instead of per-table system errors', () => {
    const alarm = Object.values(alarms).find(
      (a) => a.Properties.AlarmName === `${MOCK_PREFIX}-ddb-user-errors`,
    );
    expect(alarm).toBeDefined();
    expect(alarm.Properties.Namespace).toBe('AWS/DynamoDB');
    expect(alarm.Properties.MetricName).toBe('UserErrors');
    // Published account-wide only; no dimension set exists for it.
    expect(alarm.Properties.Dimensions).toBeUndefined();
  });

  it('creates no per-table system-error alarms', () => {
    const systemErrorAlarms = ddbAlarmNames().filter((n) => n.includes('system-error'));
    expect(systemErrorAlarms).toEqual([]);
  });

  it('every DynamoDB alarm is routed to the alarm topic', () => {
    for (const alarm of Object.values(alarms)) {
      const name = alarm.Properties.AlarmName;
      if (typeof name === 'string' && name.startsWith(`${MOCK_PREFIX}-ddb-`)) {
        expect(alarm.Properties.AlarmActions).toHaveLength(1);
        expect(alarm.Properties.OKActions).toHaveLength(1);
      }
    }
  });

  /**
   * CloudFormation caps a stack at 500 resources, and this is a deliberate
   * single-stack architecture with no second stack to spill into. Per-table
   * alarms are the largest single consumer of that budget, so the ceiling is
   * asserted here: if the stack approaches it, this test fails while there is
   * still room to react, rather than a deploy failing after CI has gone green.
   */
  it('stack stays clear of the 500-resource CloudFormation limit', () => {
    const total = Object.keys(template.toJSON().Resources).length;
    expect(total).toBeLessThan(460);
  });
});
