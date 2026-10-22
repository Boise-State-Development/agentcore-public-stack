/**
 * The Chromium URL policy the browser is started with (spec D6).
 *
 * ⚠️ Applied at session level it is RECOMMENDED-only — the service rejects
 * MANAGED — so it constrains the agent but not a human in a takeover. The
 * un-overridable version needs `CreateBrowser`.
 *
 * These tests care about two things regardless: that the document says what we
 * think it says, and that the browser can actually read it.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import {
  buildBrowserManagedPolicy,
  BROWSER_MANAGED_POLICY_KEY,
} from '../lib/constructs/agentcore/browser-policy-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

describe('buildBrowserManagedPolicy', () => {
  it('renders a Chromium URLBlocklist document', () => {
    expect(buildBrowserManagedPolicy(['a.example.com', 'b.example.com'])).toEqual({
      URLBlocklist: ['a.example.com', 'b.example.com'],
    });
  });

  it('is a blocklist, never an allowlist', () => {
    // An allowlist of assessment targets would need a new entry per VPAT
    // review, and the browser resource is immutable — that design was
    // rejected in D6 for exactly that reason.
    const policy = buildBrowserManagedPolicy(['x.example.com']);

    expect(policy).not.toHaveProperty('URLAllowlist');
  });

  it('copies the list rather than aliasing the config array', () => {
    const list = ['a.example.com'];
    const policy = buildBrowserManagedPolicy(list) as { URLBlocklist: string[] };
    list.push('mutated.example.com');

    expect(policy.URLBlocklist).toEqual(['a.example.com']);
  });

  it('renders an empty blocklist rather than omitting the key', () => {
    // An absent URLBlocklist and an empty one mean the same thing to Chromium,
    // but the explicit key makes "this environment blocks nothing" visible in
    // the deployed object instead of looking like a failed render.
    expect(buildBrowserManagedPolicy([])).toEqual({ URLBlocklist: [] });
  });
});

describe('the policy object key', () => {
  it('is not double-prefixed', () => {
    // Regression: the BucketDeployment's `destinationKeyPrefix` compounded
    // with a source name that already carried the prefix, so the object landed
    // at `policies/policies/managed-policies.json` while BROWSER_POLICY_S3
    // pointed one level up. The deploy looked clean and the policy silently
    // resolved to a missing key.
    expect(BROWSER_MANAGED_POLICY_KEY).toBe('policies/managed-policies.json');
    expect(BROWSER_MANAGED_POLICY_KEY).not.toContain('policies/policies');
  });
});

describe('browser policy in the stack', () => {
  let template: Template;

  beforeAll(() => {
    const config = createMockConfig();
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  it('versions the policy bucket so past states are auditable', () => {
    // This is a security control; what the browser was allowed to reach on a
    // given date is worth more than the storage.
    const buckets = Object.values(template.findResources('AWS::S3::Bucket'));
    const policyBucket = buckets.find((b) =>
      JSON.stringify((b.Properties as { BucketName?: unknown })?.BucketName ?? '').includes(
        'browser-policy',
      ),
    );

    expect(policyBucket).toBeDefined();
    expect((policyBucket!.Properties as { VersioningConfiguration?: { Status?: string } })
      .VersioningConfiguration?.Status).toBe('Enabled');
  });

  it('lets the browser execution role read the policy, and nothing else', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const statements = policies.flatMap(
      (p) =>
        ((p.Properties as { PolicyDocument?: { Statement?: { Sid?: string; Action?: string | string[] }[] } })
          ?.PolicyDocument?.Statement) ?? [],
    );
    const grant = statements.find((s) => s.Sid === 'BrowserEnterprisePolicyS3Access');

    expect(grant).toBeDefined();
    const actions = Array.isArray(grant!.Action) ? grant!.Action : [grant!.Action];
    expect(actions.sort()).toEqual(['s3:GetObject', 's3:GetObjectVersion']);
    // Read-only: the browser must never be able to rewrite its own policy.
    expect(actions.join(',')).not.toMatch(/PutObject|DeleteObject/);
  });

  it('lets the CALLER of StartBrowserSession read the policy object', () => {
    // The service reads the policy as the caller — the runtime role — not as
    // the browser's execution role, despite the documented prerequisite
    // naming the latter. Granting only the browser role produced
    // "Access denied to S3 object ... Verify that the caller has permission",
    // and that failure takes down EVERY browser session, not just the policy.
    // CDK splits an oversized inline policy into managed overflow policies on
    // the same role, so both resource types must be scanned.
    const statements = ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'].flatMap((type) =>
      Object.values(template.findResources(type)).flatMap(
        (p) =>
          ((p.Properties as {
            PolicyDocument?: { Statement?: { Sid?: string; Action?: string | string[] }[] };
          })?.PolicyDocument?.Statement) ?? [],
      ),
    );

    const caller = statements.find((s) => s.Sid === 'BrowserPolicyObjectRead');
    expect(caller).toBeDefined();
    const actions = Array.isArray(caller!.Action) ? caller!.Action : [caller!.Action];
    expect(actions.sort()).toEqual(['s3:GetObject', 's3:GetObjectVersion']);

    // The browser execution role keeps its own grant — the service documents
    // it as a prerequisite, and it costs one read-only object.
    expect(
      statements.find((s) => s.Sid === 'BrowserEnterprisePolicyS3Access'),
    ).toBeDefined();
  });

  it('passes the policy location to the runtime as a single S3 URI', () => {
    // Single variable on purpose — the runtime's 50-env-var ceiling is full.
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    const [runtime] = Object.values(runtimes);
    const env = (runtime.Properties as { EnvironmentVariables?: Record<string, unknown> })
      .EnvironmentVariables ?? {};

    expect(env).toHaveProperty('BROWSER_POLICY_S3');
    expect(env).not.toHaveProperty('BROWSER_POLICY_BUCKET');

    // The URI the backend is told must name the key the deployment writes.
    // A template test cannot see inside the asset zip, so assert the two are
    // derived from the same constant rather than two that drifted.
    expect(JSON.stringify(env.BROWSER_POLICY_S3)).toContain(
      BROWSER_MANAGED_POLICY_KEY,
    );
  });
});
