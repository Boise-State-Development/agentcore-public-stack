import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

/**
 * Pre-stream stage latency widgets (docs/specs/turn-latency-preamble.md).
 *
 * They live on the AgentCore Runtime dashboard rather than a dashboard of
 * their own, because `observability-platform-dashboard.test.ts` pins the stack
 * at three dashboards ($3/month beyond that) and the AWS-reported `Latency`
 * graph on that dashboard is exactly the number these decompose.
 */
describe('Turn latency widgets on the AgentCore dashboard', () => {
  let body: string;
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
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);

    // Select by DashboardName, not by searching bodies: the platform-health
    // dashboard *links* to this one, so its body contains this name too and a
    // substring search silently picks the wrong board.
    const dashboards = template.findResources('AWS::CloudWatch::Dashboard');
    const agentCore = Object.values(dashboards).find(
      (d: any) => d.Properties.DashboardName === `${MOCK_PREFIX}-agentcore-observability`,
    );
    expect(agentCore).toBeDefined();
    body = JSON.stringify((agentCore as any).Properties.DashboardBody);
  });

  it('does not add a fourth dashboard — the ceiling is a cost decision', () => {
    // CloudWatch charges $3/month beyond three. If a later change genuinely
    // needs a fourth, that is a deliberate trade to make, not a side effect of
    // adding widgets.
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 3);
  });

  it('graphs every stage turn_timing.py emits', () => {
    // A name that drifts from `_metric_name()` renders an EMPTY graph rather
    // than erroring — indistinguishable from "that stage never ran".
    for (const metric of [
      'PreludeTotalMs',
      'PreambleMs',
      'PreambleOwnershipMs',
      'PreambleSkillsMs',
      'PreambleFilesMs',
      'PreambleSessionStateMs',
      'PreambleQuotaMs',
      'AgentBuildMs',
    ]) {
      expect(body).toContain(metric);
    }
  });

  it('uses the namespace turn_timing.py defaults to', () => {
    // CDK does not set TURN_LATENCY_EMF_NAMESPACE (dev and prod are separate
    // accounts), so its default IS the contract between the two sides.
    expect(body).toContain('AgentCoreStack/TurnLatency');
  });

  it('plots percentiles — a summed latency is meaningless', () => {
    expect(body).toContain('p50');
    expect(body).toContain('p90');
    expect(body).toContain('p99');
  });

  it('slices by turn shape in Logs Insights rather than by metric dimension', () => {
    // isResume / deferredBuild ride as EMF log properties: dimensions would
    // multiply metric streams and invite reading a p99 off too thin a slice.
    expect(body).toContain('isResume');
    expect(body).toContain('deferredBuild');
  });

  it('keeps an unaccounted-time widget, so an incomplete decomposition shows', () => {
    expect(body).toContain('unaccountedMs');
  });

  it('carries the "this cannot see the app-api hop" caveat on the dashboard itself', () => {
    // The caveat must live where the number is read. Someone who sees
    // PreambleMs fall and concludes the user waits less is wrong by up to
    // ~1.5s of routing these metrics are structurally blind to, and a comment
    // in the CDK source does not reach them.
    expect(body).toContain('handler entry');
    expect(body).toContain('tests/load');
  });
});
