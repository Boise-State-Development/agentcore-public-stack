import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AlarmTopicConstruct } from '../lib/constructs/observability/alarm-topic-construct';
import { PlatformStack } from '../lib/platform-stack';
import {
  createMockConfig,
  mockSsmContext,
  MOCK_ACCOUNT,
  MOCK_PREFIX,
  MOCK_REGION,
} from './helpers/mock-config';

function synth(): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  new AlarmTopicConstruct(stack, 'AlarmTopic', { config: createMockConfig() });
  return Template.fromStack(stack);
}

describe('AlarmTopicConstruct', () => {
  let t: Template;
  beforeAll(() => {
    t = synth();
  });

  it('creates one topic with the conventional name', () => {
    t.resourceCountIs('AWS::SNS::Topic', 1);
    t.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: `${MOCK_PREFIX}-alarms`,
    });
  });

  it('encrypts the topic with a customer-managed key, not the AWS-managed one', () => {
    t.resourceCountIs('AWS::KMS::Key', 1);
    const topic = Object.values(t.findResources('AWS::SNS::Topic'))[0];
    // A Ref/Fn::GetAtt to our own key resource, never the string
    // 'alias/aws/sns' — see the next test for why that distinction matters.
    expect(topic.Properties.KmsMasterKeyId).toBeDefined();
    expect(JSON.stringify(topic.Properties.KmsMasterKeyId)).not.toContain('alias/aws/sns');
  });

  /**
   * THE test in this file.
   *
   * CloudWatch publishes alarm notifications as the `cloudwatch.amazonaws.com`
   * service principal. Against an SNS topic encrypted with the AWS-managed
   * `alias/aws/sns` key, that publish is denied and the message is dropped
   * silently: the alarm still goes to ALARM in the console, so the monitoring
   * system looks healthy at exactly the moment it has stopped delivering.
   *
   * `kms:Decrypt` alone is insufficient — SNS envelope encryption has the
   * PUBLISHER generate the data key, so GenerateDataKey* is required too.
   * Both are asserted because dropping either one reintroduces silent failure.
   */
  it('key policy lets CloudWatch generate a data key AND decrypt', () => {
    const key = Object.values(t.findResources('AWS::KMS::Key'))[0];
    const statements = key.Properties.KeyPolicy.Statement;

    const cwStatement = statements.find(
      (s: any) => s.Principal?.Service === 'cloudwatch.amazonaws.com',
    );
    expect(cwStatement).toBeDefined();
    expect(cwStatement.Effect).toBe('Allow');

    const actions: string[] = Array.isArray(cwStatement.Action)
      ? cwStatement.Action
      : [cwStatement.Action];
    expect(actions).toContain('kms:GenerateDataKey*');
    expect(actions).toContain('kms:Decrypt');
  });

  it('scopes the CloudWatch key grant to this account', () => {
    const key = Object.values(t.findResources('AWS::KMS::Key'))[0];
    const cwStatement = key.Properties.KeyPolicy.Statement.find(
      (s: any) => s.Principal?.Service === 'cloudwatch.amazonaws.com',
    );
    expect(cwStatement.Condition.StringEquals['aws:SourceAccount']).toBe(MOCK_ACCOUNT);
  });

  it('enables key rotation', () => {
    t.hasResourceProperties('AWS::KMS::Key', { EnableKeyRotation: true });
  });

  it('allows CloudWatch to publish and denies non-TLS traffic', () => {
    const policies = t.findResources('AWS::SNS::TopicPolicy');
    const doc = JSON.stringify(Object.values(policies)[0].Properties.PolicyDocument);
    expect(doc).toContain('cloudwatch.amazonaws.com');
    expect(doc).toContain('sns:Publish');
    // enforceSSL renders as a Deny on aws:SecureTransport false.
    expect(doc).toContain('SecureTransport');
  });

  /**
   * Subscriptions are deliberately NOT infrastructure-as-code. Several teams
   * need to hear about failures and their membership changes far more often
   * than the infrastructure does; requiring a PR and a CloudFormation deploy to
   * add one address is how notification lists go stale and stop being trusted.
   *
   * This assertion is the guard on that decision — if someone adds a
   * subscription here, this test explains why not to.
   */
  it('creates NO subscriptions (managed out-of-band on purpose)', () => {
    t.resourceCountIs('AWS::SNS::Subscription', 0);
  });

  it('publishes the topic ARN to SSM and as a CfnOutput for discovery', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `/${MOCK_PREFIX}/observability/alarm-topic-arn`,
    });
    const outputs = t.findOutputs('*');
    const outputJson = JSON.stringify(outputs);
    expect(outputJson).toContain('AlarmTopicArn');
    expect(outputJson).toContain(`${MOCK_PREFIX}-AlarmTopicArn`);
  });

  /**
   * The CMK wraps in-flight notifications only — it protects no durable data,
   * and alarm history lives in CloudWatch. Retaining it on stack delete would
   * strand a billable key with nothing left to decrypt, so this one key
   * deliberately does NOT follow getRemovalPolicy(config).
   */
  it('destroys the CMK on stack delete rather than stranding a billable key', () => {
    const key = Object.values(t.findResources('AWS::KMS::Key'))[0];
    expect(key.DeletionPolicy).toBe('Delete');
  });
});

/**
 * The alarmTopicEnabled gate lives in PlatformStack, not in the construct, so
 * it can only be exercised against a real stack synth.
 */
describe('PlatformStack alarm topic gating', () => {
  function synthStack(alarmTopicEnabled: boolean): { stack: PlatformStack; template: Template } {
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
    const config = {
      ...base,
      observability: { ...base.observability, alarmTopicEnabled },
    };
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    return { stack, template: Template.fromStack(stack) };
  }

  it('exposes the topic and names it {prefix}-alarms when enabled', () => {
    const { stack, template } = synthStack(true);
    expect(stack.alarmTopic).toBeDefined();
    template.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: `${MOCK_PREFIX}-alarms`,
    });
  });

  /**
   * The opt-out path. A fork that routes alerts some other way gets no topic,
   * no CMK, and no alarm actions — which is this stack's pre-existing
   * console-only behaviour, deliberately kept reachable rather than removed.
   */
  it('creates no topic, no CMK, and no alarm actions when disabled', () => {
    const { stack, template } = synthStack(false);
    expect(stack.alarmTopic).toBeUndefined();

    const topics = template.findResources('AWS::SNS::Topic');
    expect(Object.keys(topics)).toHaveLength(0);

    // No key aliased for the alarm topic. Other CMKs in the stack (BFF cookie
    // key, OAuth token key) are unaffected, so assert on the alias rather than
    // on a bare resource count.
    const aliases = template.findResources('AWS::KMS::Alias');
    const aliasNames = Object.values(aliases).map((a: any) => a.Properties.AliasName);
    expect(aliasNames).not.toContain(`alias/${MOCK_PREFIX}-alarm-topic-key`);

    for (const alarm of Object.values(template.findResources('AWS::CloudWatch::Alarm'))) {
      expect((alarm as any).Properties.AlarmActions).toBeUndefined();
    }
  });
});
