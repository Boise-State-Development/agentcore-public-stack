/**
 * Transport-security baseline tests.
 *
 * Asserts that the synthesized infrastructure pins a modern TLS policy
 * on the two viewer-facing surfaces — the SPA's CloudFront distribution
 * and the application load balancer's HTTPS listener — so legacy CBC
 * ciphers and TLS 1.0/1.1 are not on the menu.
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

import { AlbConstruct } from '../lib/constructs/network/alb-construct';
import { SpaBucketConstruct } from '../lib/constructs/spa/spa-bucket-construct';
import { SpaDistributionConstruct } from '../lib/constructs/spa/spa-distribution-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

describe('Transport security baseline', () => {
  describe('ALB HTTPS listener', () => {
    it('uses a TLS 1.2+ security policy on the HTTPS listener', () => {
      const stack = testStack();
      const config = createMockConfig({
        certificateArn:
          'arn:aws:acm:us-east-1:123456789012:certificate/test',
      });
      const vpc = new ec2.Vpc(stack, 'Vpc');
      new AlbConstruct(stack, 'Alb', { config, vpc });
      const t = Template.fromStack(stack);

      // The HTTPS listener must pin SslPolicy to a TLS-1.2-minimum
      // policy. The default (older 2016-08 family) silently allows
      // TLS 1.0 + CBC, which is exactly what scanners flag.
      t.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
        Protocol: 'HTTPS',
        SslPolicy: Match.stringLikeRegexp('TLS13.*-2021-06|TLS-1-2-2017-01'),
      });
    });
  });

  describe('CloudFront distribution', () => {
    it('pins minimum TLS protocol to 1.2+ on the SPA distribution', () => {
      const stack = testStack();
      const config = createMockConfig({
        domainName: 'example.com',
      });
      // The distribution only attaches a certificate (and therefore
      // sets ViewerCertificate.MinimumProtocolVersion) when the
      // frontend.certificateArn is also configured.
      (config.frontend as any).certificateArn =
        'arn:aws:acm:us-east-1:123456789012:certificate/test';
      const bucket = new SpaBucketConstruct(stack, 'Bucket', { config });
      new SpaDistributionConstruct(stack, 'Dist', {
        config,
        bucket: bucket.bucket,
        appApiUrl: 'https://api.example.com',
      });

      const t = Template.fromStack(stack);

      // CloudFront's MinimumProtocolVersion must be a 2021-vintage
      // policy (TLSv1.2_2021 — drops TLS 1.0/1.1 entirely and prunes
      // BEAST-vulnerable CBC ciphers).
      t.hasResourceProperties('AWS::CloudFront::Distribution', {
        DistributionConfig: Match.objectLike({
          ViewerCertificate: Match.objectLike({
            MinimumProtocolVersion: 'TLSv1.2_2021',
          }),
        }),
      });
    });
  });
});
