import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

/**
 * Alarm routing guard.
 *
 * ## The failure this exists to prevent
 *
 * Before this work, PlatformStack had 13 CloudWatch alarms and not one of them
 * notified anybody. Three separate constructs carried a comment saying "no SNS
 * wiring in this stack yet". Nobody had been careless: `new cloudwatch.Alarm()`
 * is the obvious API, and it produces a console-only alarm that looks entirely
 * complete. An alarm with no action still turns red in the console, so the gap
 * is invisible from the one place an operator would look to check.
 *
 * A convention cannot protect against that, because the broken form is the
 * shorter one. So the protection is mechanical: this file fails if ANY alarm in
 * the synthesized template lacks an action.
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
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
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

  it('synthesizes a substantial number of alarms', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    // Sanity floor: if this drops sharply, alarms were deleted rather than the
    // guard below being satisfied trivially by an empty set.
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

  /**
   * Recovery notifications matter as much as the alarm itself: an operator who
   * was paged and never told the condition cleared has to go and check the
   * console, which is the behaviour this whole effort exists to remove.
   */
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
 * Static source guard.
 *
 * The template guard above only sees alarms that a synth actually produces. An
 * alarm behind a feature flag that no test enables would slip past it. This
 * catches the raw constructor at the source level instead, so the rule holds
 * for code paths the tests do not reach.
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
      // The factory is the one legitimate caller: it is where the SNS action
      // gets attached.
      if (file.endsWith(path.join('observability', 'alarm-factory.ts'))) continue;

      const source = fs.readFileSync(file, 'utf-8');
      if (/new\s+cloudwatch\.Alarm\s*\(/.test(source)) {
        offenders.push(path.relative(libDir, file));
      }
    }

    expect(offenders).toEqual([]);
  });

  /**
   * The single-value rule. This repo is forked by many institutions: a fork with
   * one environment should not have to reason about a `production` boolean, and
   * a fork with three should not be limited to two. Environment differences
   * belong in the forker's deployment config, reaching the code as a single
   * configured value.
   */
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
