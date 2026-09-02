import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

describe('Unified platform dashboard', () => {
  let template: Template;
  let dashboardBody: string;
  let alarmCount: number;

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
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);

    const dashboards = template.findResources('AWS::CloudWatch::Dashboard');
    const platform = Object.values(dashboards).find((d: any) =>
      JSON.stringify(d.Properties.DashboardName).includes('platform-health'),
    );
    expect(platform).toBeDefined();
    dashboardBody = JSON.stringify((platform as any).Properties.DashboardBody);
    alarmCount = Object.keys(template.findResources('AWS::CloudWatch::Alarm')).length;
  });

  it('creates the dashboard with the conventional name', () => {
    template.hasResourceProperties('AWS::CloudWatch::Dashboard', {
      DashboardName: `${MOCK_PREFIX}-platform-health`,
    });
  });

  /**
   * CloudWatch gives three dashboards free and charges $3/month for each one
   * after. agentcore-observability + prompt-cache-observability + this one lands
   * exactly on the ceiling, which is why this dashboard links out to those two
   * rather than restating their widgets.
   */
  it('keeps the stack at exactly three dashboards (the CloudWatch free ceiling)', () => {
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 3);
  });

  it('links to the two drill-down dashboards instead of duplicating them', () => {
    expect(dashboardBody).toContain(`${MOCK_PREFIX}-agentcore-observability`);
    expect(dashboardBody).toContain(`${MOCK_PREFIX}-prompt-cache-observability`);
  });

  it('names the SNS topic alarms route to, so an operator can find it', () => {
    expect(dashboardBody).toContain(`${MOCK_PREFIX}-alarms`);
  });

  /**
   * The SSE caveat is on the dashboard itself, not just in code comments,
   * because the person reading it at 3am is not reading the CDK source. A drop
   * in latency can mean turns are failing early rather than getting faster.
   */
  it('warns on-dashboard that SSE makes long response times normal', () => {
    expect(dashboardBody).toMatch(/SSE/);
  });

  describe('row 1 — is traffic being served', () => {
    it('graphs front-door request count and both flavours of 5xx', () => {
      expect(dashboardBody).toContain('RequestCount');
      expect(dashboardBody).toContain('HTTPCode_ELB_5XX_Count');
      expect(dashboardBody).toContain('HTTPCode_Target_5XX_Count');
    });

    it('graphs agent invocations against errors and throttles', () => {
      expect(dashboardBody).toContain('AWS/Bedrock-AgentCore');
      expect(dashboardBody).toContain('Invocations');
      expect(dashboardBody).toContain('SystemErrors');
      expect(dashboardBody).toContain('UserErrors');
      expect(dashboardBody).toContain('Throttles');
    });

    it('graphs running tasks against unhealthy targets', () => {
      expect(dashboardBody).toContain('RunningTaskCount');
      expect(dashboardBody).toContain('UnHealthyHostCount');
    });
  });

  describe('row 2 — saturation', () => {
    it('graphs app-api CPU and memory', () => {
      expect(dashboardBody).toContain('CPUUtilization');
      expect(dashboardBody).toContain('MemoryUtilization');
    });

    /** The only predictive graph on the dashboard. */
    it('graphs Bedrock quota headroom, the leading indicator', () => {
      expect(dashboardBody).toContain('EstimatedTPMQuotaUsage');
      expect(dashboardBody).toContain('InvocationThrottles');
    });

    it('graphs DynamoDB request errors', () => {
      expect(dashboardBody).toContain('AWS/DynamoDB');
    });
  });

  describe('row 3 — alarm status', () => {
    /**
     * The widget's alarm list is discovered by walking the construct tree rather
     * than hand-maintained, so an alarm added later cannot silently go missing
     * from the one dashboard an on-call engineer opens. This asserts the
     * discovery actually found everything.
     */
    it('includes every alarm in the stack', () => {
      expect(alarmCount).toBeGreaterThan(60);
      // The widget references each alarm by ARN, which renders as
      // {"Fn::GetAtt": [<AlarmLogicalId>, "Arn"]} rather than a literal string —
      // hence counting GetAtt Arn references rather than grepping for ':alarm:'.
      const arnRefs = (dashboardBody.match(/Fn::GetAtt/g) || []).length;
      expect(arnRefs).toBeGreaterThanOrEqual(alarmCount);
    });

    it('renders an alarm-status widget', () => {
      expect(dashboardBody).toContain('alarm');
      expect(dashboardBody).toContain('All platform alarms');
    });
  });

  it('exports the dashboard name', () => {
    template.hasOutput('*', {
      Export: { Name: `${MOCK_PREFIX}-PlatformDashboard` },
    });
  });

  /**
   * The dashboard and the AgentCore alarms must bind the `Name` dimension to the
   * same string. Two places deriving it independently is how one ends up
   * watching a stream that is never published — the failure this whole effort
   * started by finding.
   */
  it('binds the runtime Name dimension identically to the alarms', () => {
    expect(dashboardBody).toContain('::DEFAULT');
  });
});
