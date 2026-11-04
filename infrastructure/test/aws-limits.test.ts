/**
 * AWS Limits (GitHub issue #12): the App API task role's Service Quotas grants
 * and the AWS_LIMITS_ENABLED variable exist only with CDK_AWS_LIMITS_ENABLED on.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { AppConfig } from '../lib/config';
import { AWS_LIMITS_REQUESTABLE_SERVICES } from '../lib/constructs/app-api/app-api-iam-grants';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const SIDS = ['AwsLimitsRead', 'AwsLimitsMetricsRead', 'AwsLimitsRequestIncrease', 'AwsLimitsServiceLinkedRole'];

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

interface Statement {
  Sid?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
}

/** Every statement in every inline and managed policy, keyed by Sid. */
function statementsBySid(template: Template): Map<string, Statement> {
  const out = new Map<string, Statement>();
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const resource of Object.values(template.findResources(type)) as any[]) {
      for (const s of resource.Properties.PolicyDocument.Statement as Statement[]) {
        if (s.Sid) out.set(s.Sid, s);
      }
    }
  }
  return out;
}

function appApiEnv(template: Template): Record<string, unknown> {
  const env: Record<string, unknown> = {};
  for (const td of Object.values(template.findResources('AWS::ECS::TaskDefinition')) as any[]) {
    for (const container of td.Properties.ContainerDefinitions ?? []) {
      for (const entry of container.Environment ?? []) env[entry.Name] = entry.Value;
    }
  }
  return env;
}

describe('AWS Limits in PlatformStack', () => {
  const off = synth();
  const on = synth({ awsLimits: { enabled: true } });

  it('grants nothing and sets the flag off by default', () => {
    const sids = statementsBySid(off);
    for (const sid of SIDS) expect(sids.has(sid)).toBe(false);
    expect(appApiEnv(off).AWS_LIMITS_ENABLED).toBe('false');
  });

  it('adds the four statements and the flag when on, and no resources', () => {
    const sids = statementsBySid(on);
    for (const sid of SIDS) expect(sids.has(sid)).toBe(true);
    expect(appApiEnv(on).AWS_LIMITS_ENABLED).toBe('true');
    const count = (t: Template) => Object.keys(t.toJSON().Resources).length;
    expect(count(on)).toBe(count(off));
  });

  it('limits increase requests to the monitored services', () => {
    const write = statementsBySid(on).get('AwsLimitsRequestIncrease')!;
    expect(write.Action).toBe('servicequotas:RequestServiceQuotaIncrease');
    expect(write.Condition?.StringEquals['servicequotas:service']).toEqual(AWS_LIMITS_REQUESTABLE_SERVICES);
    expect(AWS_LIMITS_REQUESTABLE_SERVICES).not.toContain('ec2');
  });

  it('reads metrics with GetMetricData and ListMetrics only', () => {
    const metrics = statementsBySid(on).get('AwsLimitsMetricsRead')!;
    expect(metrics.Action).toEqual(['cloudwatch:GetMetricData', 'cloudwatch:ListMetrics']);
    expect(metrics.Resource).toBe('*');
  });

  it('lets only the Service Quotas service-linked role be created', () => {
    const slr = statementsBySid(on).get('AwsLimitsServiceLinkedRole')!;
    expect(slr.Action).toBe('iam:CreateServiceLinkedRole');
    expect(slr.Resource).toBe(
      'arn:aws:iam::*:role/aws-service-role/servicequotas.amazonaws.com/AWSServiceRoleForServiceQuotas',
    );
    expect(slr.Condition?.StringEquals['iam:AWSServiceName']).toBe('servicequotas.amazonaws.com');
  });

  it('grants no support:* actions', () => {
    for (const s of statementsBySid(on).values()) {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action ?? ''];
      expect(actions.some((a) => a.startsWith('support:'))).toBe(false);
    }
  });
});
