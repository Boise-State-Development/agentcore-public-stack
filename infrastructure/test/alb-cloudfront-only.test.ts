// ALB ingress is CloudFront-only across the whole synthesized stack.
//
// The construct-level tests pin the ALB security group's own rules. This
// one guards what they cannot see: any other construct in PlatformStack
// (a listener left `open`, a target group, a later feature) adding a
// 0.0.0.0/0 rule to the ALB security group after the fact. A prod log
// audit found scanners hitting the ALB directly while it was open, so a
// regression here is a real exposure, not a style issue.
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AppConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function synth(overrides: Partial<AppConfig> = {}): Template {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

/** Logical id of the ALB's security group (the one the ALB is attached to). */
function albSecurityGroupId(t: Template): string {
  const albs = Object.values(t.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer'));
  expect(albs).toHaveLength(1);
  const groups = (albs[0] as any).Properties.SecurityGroups;
  expect(groups).toHaveLength(1);
  return groups[0]['Fn::GetAtt'][0];
}

/** Every ingress rule that applies to the given security group. */
function ingressFor(t: Template, sgId: string): any[] {
  const sg = t.toJSON().Resources[sgId];
  const inline = sg.Properties.SecurityGroupIngress ?? [];
  const standalone = Object.values(t.findResources('AWS::EC2::SecurityGroupIngress'))
    .map((r: any) => r.Properties)
    .filter((p: any) => JSON.stringify(p.GroupId).includes(sgId));
  return [...inline, ...standalone];
}

describe.each([
  ['without an ALB certificate', {}, 80],
  [
    'with an ALB certificate',
    { certificateArn: `arn:aws:acm:${MOCK_REGION}:${MOCK_ACCOUNT}:certificate/abc` },
    443,
  ],
])('PlatformStack ALB ingress %s', (_label, overrides, port) => {
  let t: Template;
  let rules: any[];
  beforeAll(() => {
    t = synth(overrides as Partial<AppConfig>);
    rules = ingressFor(t, albSecurityGroupId(t));
  });

  it('has no internet-wide ingress rule', () => {
    expect(rules.filter((r) => r.CidrIp === '0.0.0.0/0' || r.CidrIpv6 === '::/0')).toEqual([]);
  });

  it(`admits exactly one rule: the CloudFront prefix list on :${port}`, () => {
    expect(rules).toHaveLength(1);
    expect(rules[0]).toMatchObject({ IpProtocol: 'tcp', FromPort: port, ToPort: port });
    expect(JSON.stringify(rules[0].SourcePrefixListId)).toContain('CloudFrontOriginFacingPrefixList');
  });
});

describe('PlatformStack ALB ingress with albRestrictToCloudFront=false', () => {
  it('is reachable from the internet (the explicit opt-out)', () => {
    const t = synth({ albRestrictToCloudFront: false });
    const rules = ingressFor(t, albSecurityGroupId(t));
    expect(rules.some((r) => r.CidrIp === '0.0.0.0/0')).toBe(true);
    expect(rules.some((r) => r.SourcePrefixListId)).toBe(false);
  });
});
