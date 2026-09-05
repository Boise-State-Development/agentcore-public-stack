import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

/**
 * Fails if any alarm in the template lacks an action. An unrouted alarm still
 * turns red in the console, so the gap is invisible from the one place an
 * operator would look — a convention cannot protect against that.
 */
describe('Alarm routing — every alarm reaches a human', () => {
  let template: Template;

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
      // Every optional alarm-bearing subsystem ON, so the guard sees the
      // widest possible set of alarms rather than only the always-on ones.
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
  });

  // Floor, so the guard below cannot pass trivially on an empty set.
  it('synthesizes a substantial number of alarms', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(13);
  });

  /** THE guard. */
  it('every alarm has a non-empty AlarmActions', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const unrouted: string[] = [];

    for (const [logicalId, alarm] of Object.entries(alarms)) {
      const actions = (alarm as any).Properties?.AlarmActions;
      if (!Array.isArray(actions) || actions.length === 0) {
        const name = (alarm as any).Properties?.AlarmName;
        unrouted.push(`${logicalId} (${JSON.stringify(name)})`);
      }
    }

    expect(unrouted).toEqual([]);
  });

  it('every alarm also notifies on recovery (OKActions)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const noOk: string[] = [];

    for (const [logicalId, alarm] of Object.entries(alarms)) {
      const actions = (alarm as any).Properties?.OKActions;
      if (!Array.isArray(actions) || actions.length === 0) {
        noOk.push(logicalId);
      }
    }

    expect(noOk).toEqual([]);
  });

  it('all alarm actions point at the single platform alarm topic', () => {
    const topics = template.findResources('AWS::SNS::Topic');
    expect(Object.keys(topics)).toHaveLength(1);
    const topicLogicalId = Object.keys(topics)[0];

    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    for (const alarm of Object.values(alarms)) {
      for (const action of (alarm as any).Properties.AlarmActions) {
        // Each action is { Ref: <topic logical id> }.
        expect(JSON.stringify(action)).toContain(topicLogicalId);
      }
    }
  });

  it('every alarm has a name carrying the project prefix', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    for (const [logicalId, alarm] of Object.entries(alarms)) {
      const name = (alarm as any).Properties?.AlarmName;
      expect(typeof name === 'string' ? name : JSON.stringify(name)).toContain(
        'test-project',
      );
      expect(logicalId).toBeTruthy();
    }
  });
});

/**
 * Source-level guards, which also cover flag-gated code paths a synth never
 * reaches.
 */
describe('Alarm routing — source-level guard', () => {
  const libDir = path.join(__dirname, '..', 'lib');

  function walk(dir: string): string[] {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return walk(full);
      return entry.isFile() && entry.name.endsWith('.ts') ? [full] : [];
    });
  }

  it('no construct calls new cloudwatch.Alarm() directly — use AlarmFactory', () => {
    const offenders: string[] = [];

    for (const file of walk(libDir)) {
      if (file.endsWith(path.join('observability', 'alarm-factory.ts'))) continue;

      const source = fs.readFileSync(file, 'utf-8');
      if (/new\s+cloudwatch\.Alarm\s*\(/.test(source)) {
        offenders.push(path.relative(libDir, file));
      }
    }

    expect(offenders).toEqual([]);
  });

  it('no config.production branching in the observability constructs', () => {
    const obsDir = path.join(libDir, 'constructs', 'observability');
    const offenders: string[] = [];

    for (const file of walk(obsDir)) {
      const source = fs.readFileSync(file, 'utf-8');
      if (/config\.production/.test(source)) {
        offenders.push(path.relative(obsDir, file));
      }
    }

    expect(offenders).toEqual([]);
  });

  it('the stale "no SNS wiring yet" comments are gone', () => {
    const offenders: string[] = [];
    for (const file of walk(libDir)) {
      const source = fs.readFileSync(file, 'utf-8');
      if (/no SNS (topics|wiring)/i.test(source)) {
        offenders.push(path.relative(libDir, file));
      }
    }
    expect(offenders).toEqual([]);
  });
});
