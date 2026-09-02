import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as fs from 'fs';
import * as path from 'path';

import { AppConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function synth(logRetentionDays: number): Template {
  const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
  const base = createMockConfig({
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
  const config: AppConfig = {
    ...base,
    observability: { ...base.observability, logRetentionDays },
  };
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

describe('Log retention — one configured value everywhere', () => {
  it('every log group uses the configured retention', () => {
    const template = synth(30);
    const groups = template.findResources('AWS::Logs::LogGroup');
    expect(Object.keys(groups).length).toBeGreaterThan(10);

    for (const [logicalId, group] of Object.entries(groups)) {
      expect((group as any).Properties.RetentionInDays).toBe(30);
      expect(logicalId).toBeTruthy();
    }
  });

  it('changing the configured value moves every log group together', () => {
    const groups = synth(90).findResources('AWS::Logs::LogGroup');
    const values = new Set(
      Object.values(groups).map((g: any) => g.Properties.RetentionInDays),
    );
    expect(values).toEqual(new Set([90]));
  });

  it('accepts the full range of CloudWatch retention values', () => {
    for (const days of [1, 7, 30, 365, 3653]) {
      const groups = synth(days).findResources('AWS::Logs::LogGroup');
      const values = new Set(
        Object.values(groups).map((g: any) => g.Properties.RetentionInDays),
      );
      expect(values).toEqual(new Set([days]));
    }
  });

  // CDK gives its own provider Lambdas a 731-day default, and those groups are
  // declared nowhere here. A bare cdk.App lacks the cdk.json feature flags that
  // materialise them, so this stands one in instead.
  it('overrides CDK-generated log groups that default to 731 days', () => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const base = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: { enabled: true, defaultQuotaHours: 0 },
    });
    const config: AppConfig = {
      ...base,
      observability: { ...base.observability, logRetentionDays: 30 },
    };
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();

    // Stand in for a CDK-managed group created with its own default retention.
    new logs.CfnLogGroup(stack, 'PretendCdkManagedGroup', {
      retentionInDays: 731,
    });

    const groups = Template.fromStack(stack).findResources('AWS::Logs::LogGroup');
    const values = new Set(
      Object.values(groups).map((g: any) => g.Properties.RetentionInDays),
    );
    // The Aspect rewrote it: no 731 survives anywhere.
    expect(values).toEqual(new Set([30]));
  });

  // Service-created, so a CDK LogGroup construct cannot set its retention.
  describe('service-created AgentCore Runtime log group', () => {
    it('applies retention via a custom resource', () => {
      const template = synth(30);
      const customResources = template.findResources('Custom::AWS');
      const retentionResource = Object.values(customResources).find((r: any) =>
        JSON.stringify(r.Properties).includes('putRetentionPolicy'),
      );
      expect(retentionResource).toBeDefined();

      const props = JSON.stringify((retentionResource as any).Properties);
      expect(props).toContain('CloudWatchLogs');
      expect(props).toContain('/aws/bedrock-agentcore/runtimes/');
      expect(props).toContain('-DEFAULT');
      // The call payload is assembled with Fn::Join, so the inner JSON arrives
      // backslash-escaped. Match on the key/value pair rather than an exact
      // literal so this does not break on escaping depth.
      expect(props).toMatch(/retentionInDays\\*":30/);
    });

    it('re-applies on update, not just on create', () => {
      const template = synth(30);
      const customResources = template.findResources('Custom::AWS');
      const retentionResource = Object.values(customResources).find((r: any) =>
        JSON.stringify(r.Properties).includes('putRetentionPolicy'),
      ) as any;
      expect(retentionResource.Properties.Create).toBeDefined();
      expect(retentionResource.Properties.Update).toBeDefined();
    });

    it('varies its physical id with the retention value', () => {
      const idFor = (days: number) => {
        const customResources = synth(days).findResources('Custom::AWS');
        const r = Object.values(customResources).find((x: any) =>
          JSON.stringify(x.Properties).includes('putRetentionPolicy'),
        ) as any;
        return JSON.stringify(r.Properties.Create);
      };
      expect(idFor(30)).not.toBe(idFor(90));
    });

    it('scopes its IAM policy to the runtime log group only', () => {
      const template = synth(30);
      const policies = template.findResources('AWS::IAM::Policy');
      const retentionPolicy = Object.values(policies).find((p: any) =>
        JSON.stringify(p.Properties.PolicyDocument).includes('logs:PutRetentionPolicy'),
      ) as any;
      expect(retentionPolicy).toBeDefined();
      const doc = JSON.stringify(retentionPolicy.Properties.PolicyDocument);
      // Not a wildcard across every log group in the account.
      expect(doc).toContain('/aws/bedrock-agentcore/runtimes/');
      expect(doc).not.toContain('"Resource":"*"');
    });
  });
});

/** Source guard, covering flag-gated paths a synth never reaches. */
describe('Log retention — source guard', () => {
  const libDir = path.join(__dirname, '..', 'lib');

  function walk(dir: string): string[] {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return walk(full);
      return entry.isFile() && entry.name.endsWith('.ts') ? [full] : [];
    });
  }

  it('no construct hardcodes a RetentionDays value', () => {
    const offenders: string[] = [];
    for (const file of walk(libDir)) {
      if (file.endsWith(path.join('observability', 'log-retention.ts'))) continue;
      const source = fs.readFileSync(file, 'utf-8');
      if (/retention:\s*logs\.RetentionDays\./.test(source)) {
        offenders.push(path.relative(libDir, file));
      }
    }
    expect(offenders).toEqual([]);
  });
});
