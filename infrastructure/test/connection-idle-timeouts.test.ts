/**
 * Pins the order of connection idle timeouts on the app-api path:
 *
 *   CloudFront origin keep-alive < ALB idle timeout < uvicorn --timeout-keep-alive
 *
 * Each hop that pools connections must give up an idle one before the hop
 * behind it closes it. If it doesn't, it occasionally sends a request on a
 * socket the far side has just closed, and the user gets a 502 for a request
 * that never reached app-api. On dev, 2026-11-11, CloudFront's keep-alive
 * equalled the ALB's idle timeout (60 s each), and 1 of 20 parallel presigns
 * failed this way (`AbortedOrigin`). See ALB_IDLE_TIMEOUT_SECONDS.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import {
  ALB_IDLE_TIMEOUT_SECONDS,
  CLOUDFRONT_ORIGIN_KEEPALIVE_SECONDS,
} from '../lib/constructs/network/alb-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const APP_API_DOCKERFILE = path.join(__dirname, '..', '..', 'backend', 'Dockerfile.app-api');

function synth(): Template {
  const config = createMockConfig({});
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'IdleTimeoutsPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  return Template.fromStack(stack);
}

/** The `--timeout-keep-alive` value in the app-api image's CMD. */
function uvicornKeepAliveSeconds(): number {
  const dockerfile = fs.readFileSync(APP_API_DOCKERFILE, 'utf8');
  const cmd = dockerfile.split('\n').find((line) => line.startsWith('CMD '));
  expect(cmd).toBeDefined();
  const args: string[] = JSON.parse(cmd!.slice('CMD '.length));
  const flag = args.indexOf('--timeout-keep-alive');
  // Absent means uvicorn's 5 s default, which is shorter than the ALB's.
  expect(flag).toBeGreaterThan(-1);
  return Number(args[flag + 1]);
}

describe('connection idle timeouts on the app-api path', () => {
  let template: Template;

  beforeAll(() => {
    template = synth();
  });

  it('the ALB idle timeout is the constant', () => {
    const albs = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer'));
    expect(albs).toHaveLength(1);
    const attributes: Array<{ Key: string; Value: string }> = albs[0].Properties.LoadBalancerAttributes;
    const idle = attributes.find((a) => a.Key === 'idle_timeout.timeout_seconds');
    expect(idle?.Value).toBe(String(ALB_IDLE_TIMEOUT_SECONDS));
  });

  it('the CloudFront origin in front of the ALB keeps connections for less than the ALB idle timeout', () => {
    const [albId] = Object.keys(template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer'));
    const albOrigins = Object.values(template.findResources('AWS::CloudFront::Distribution'))
      .flatMap((d) => d.Properties.DistributionConfig.Origins ?? [])
      .filter((o: { DomainName: unknown }) =>
        JSON.stringify(o.DomainName).includes(`"Fn::GetAtt":["${albId}","DNSName"]`),
      );
    expect(albOrigins.length).toBeGreaterThan(0);
    for (const origin of albOrigins) {
      expect(origin.CustomOriginConfig.OriginKeepaliveTimeout).toBe(CLOUDFRONT_ORIGIN_KEEPALIVE_SECONDS);
    }
    expect(CLOUDFRONT_ORIGIN_KEEPALIVE_SECONDS).toBeLessThan(ALB_IDLE_TIMEOUT_SECONDS);
  });

  it("uvicorn's keep-alive outlasts the ALB idle timeout", () => {
    expect(uvicornKeepAliveSeconds()).toBeGreaterThan(ALB_IDLE_TIMEOUT_SECONDS);
  });
});
