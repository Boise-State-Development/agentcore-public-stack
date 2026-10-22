/**
 * app-api's grant to start the memory maintenance worker (Shared Projects 2.6).
 *
 * The first 2.6a deploy to dev rolled back: `grantInvoke` on the app-api task
 * role put the statement in one of the role's CDK overflow policies, whose
 * resolved document (already 5,921 of IAM's 6,144 characters) then passed the
 * limit. CDK packs overflow statements by a size it estimates before ARNs
 * resolve, so a synth can't see that. The grant now has its own managed policy.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface PolicyDocument {
  Statement?: Array<{ Action?: string | string[] }>;
}

function invokesWorker(doc: PolicyDocument | undefined): boolean {
  return (doc?.Statement ?? []).some(
    (st) =>
      (Array.isArray(st.Action) ? st.Action : [st.Action]).includes('lambda:InvokeFunction') &&
      JSON.stringify(st).includes('MemoryMaintenanceWorkerLambda'),
  );
}

describe('Memory maintenance invoke grant', () => {
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

  it('is a one-statement managed policy on the app-api task role', () => {
    const policies = Object.entries(template.findResources('AWS::IAM::ManagedPolicy')).filter(([id]) =>
      id.includes('MemoryMaintenanceInvokePolicy'),
    );
    expect(policies).toHaveLength(1);
    const [, policy] = policies[0];
    expect(policy.Properties.PolicyDocument.Statement).toHaveLength(1);
    expect(invokesWorker(policy.Properties.PolicyDocument)).toBe(true);
    expect(JSON.stringify(policy.Properties.Roles)).toContain('AppApiTaskDefinitionTaskRole');
  });

  it('never lands in the task role overflow or default policies', () => {
    const appApiPolicies = [
      ...Object.entries(template.findResources('AWS::IAM::ManagedPolicy')),
      ...Object.entries(template.findResources('AWS::IAM::Policy')),
    ].filter(([id]) => id.includes('AppApiTaskDefinitionTaskRole'));
    // Non-vacuous: the role's default and overflow policies must be in the template.
    expect(appApiPolicies.length).toBeGreaterThan(0);
    for (const [, r] of appApiPolicies) expect(invokesWorker(r.Properties.PolicyDocument)).toBe(false);
  });
});
