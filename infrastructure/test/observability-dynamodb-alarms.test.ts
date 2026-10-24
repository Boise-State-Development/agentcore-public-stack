import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

describe('DynamoDB grouped throttle alarms', () => {
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
    tableCount = Object.keys(template.findResources('AWS::DynamoDB::Table')).length;
  });

  function throttleGroups(): any[] {
    return Object.values(alarms).filter((a) => (
      typeof a.Properties.AlarmName === 'string'
      && a.Properties.AlarmName.startsWith(`${MOCK_PREFIX}-ddb-throttle-group-`)
    ));
  }

  /** Logical ids of the tables a grouped alarm's metrics are bound to. */
  function tablesIn(alarm: any): string[] {
    return alarm.Properties.Metrics
      .filter((m: any) => m.MetricStat)
      .map((m: any) => m.MetricStat.Metric.Dimensions[0].Value.Ref);
  }

  // Tied to the real table count, so a new table without an alarm fails here.
  it('groups every table into throttle alarms of at most five tables', () => {
    expect(tableCount).toBe(29);
    // ceil(29 / 5) grouped throttle alarms + 1 account-level UserErrors alarm.
    expect(throttleGroups()).toHaveLength(6);
    expect(ddbAlarmNames()).toHaveLength(6 + 1);
  });

  it('every table is covered by exactly one throttle group', () => {
    const tableIds = Object.keys(template.findResources('AWS::DynamoDB::Table'));
    const covered = throttleGroups().flatMap(tablesIn);
    for (const id of tableIds) {
      // Two metrics (reads, writes) per table, all in the same group.
      expect(covered.filter((c) => c === id)).toHaveLength(2);
    }
    expect(new Set(covered).size).toBe(tableIds.length);
  });

  it('the description names every table its group covers', () => {
    const tableNames = Object.fromEntries(
      Object.entries(template.findResources('AWS::DynamoDB::Table')).map(([id, t]: [string, any]) => (
        [id, (t.Properties.TableName as string).replace(`${MOCK_PREFIX}-`, '')]
      )),
    );
    for (const alarm of throttleGroups()) {
      for (const id of new Set(tablesIn(alarm))) {
        expect(alarm.Properties.AlarmDescription).toContain(tableNames[id]);
      }
    }
  });

  // MAX over per-table sums keeps the threshold per table, not per group; FILL
  // keeps a table that only throttles reads (or only writes) from being dropped.
  it('alarms on the worst table, summing its filled read and write events', () => {
    const alarm = throttleGroups()[0];
    expect(alarm.Properties.MetricName).toBeUndefined();
    const json = JSON.stringify(alarm.Properties.Metrics);
    expect(json).toContain('ReadThrottleEvents');
    expect(json).toContain('WriteThrottleEvents');
    const expression = alarm.Properties.Metrics.find((m: any) => m.Expression).Expression;
    expect(expression).toMatch(/^MAX\(\[FILL\(r0, 0\) \+ FILL\(w0, 0\), /);
    expect(alarm.Properties.Threshold).toBe(10); // configured default
    expect(alarm.Properties.TreatMissingData).toBe('notBreaching');
  });

  // CloudWatch caps math-expression alarms at 10 metrics.
  it('no throttle group exceeds the 10-metric math limit', () => {
    for (const alarm of throttleGroups()) {
      expect(alarm.Properties.Metrics.filter((m: any) => m.MetricStat).length)
        .toBeLessThanOrEqual(10);
    }
  });

  it('throttle alarms bind TableName to the real table resource', () => {
    for (const alarm of throttleGroups()) {
      for (const m of alarm.Properties.Metrics.filter((x: any) => x.MetricStat)) {
        const dims = m.MetricStat.Metric.Dimensions;
        expect(dims).toHaveLength(1);
        expect(dims[0].Name).toBe('TableName');
        expect(JSON.stringify(dims[0].Value)).toMatch(/Ref|Fn::GetAtt/);
      }
    }
  });

  // Replaces 26 per-table SystemErrors alarms: that metric had zero streams in
  // the live account, while account-level UserErrors had real data.
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
});
