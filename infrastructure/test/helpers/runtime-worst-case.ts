/**
 * Synthesize the PlatformStack with every conditional Runtime environment
 * block turned on, so a guard that reads the Runtime's variables sees the
 * largest payload any real environment could have.
 *
 * Mirrors the worst-case config in runtime-env-var-limit.test.ts; a new
 * conditional block must be enabled here too, or guards built on this helper
 * quietly under-count.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AppConfig } from '../../lib/config';
import { PlatformStack } from '../../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './mock-config';

export interface WorstCaseSynth {
  config: AppConfig;
  stack: PlatformStack;
  template: Template;
}

export function synthWorstCasePlatformStack(overrides: Partial<AppConfig> = {}): WorstCaseSynth {
  const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
  const config = createMockConfig({
    domainName: 'example.com',
    infrastructureHostedZoneDomain: 'example.com',
    certificateArn: cert,
    frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
    artifacts: { shareInboxEnabled: false, retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
    mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
    fineTuning: { enabled: true, defaultQuotaHours: 0 },
    tokenExchange: { url: 'https://tokens.example.com/v2/oauth/token', clientId: 'test-client' },
    ...overrides,
  });
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return { config, stack, template: Template.fromStack(stack) };
}

/** The single Runtime's resolved EnvironmentVariables from a synthesized template. */
export function runtimeEnvironment(template: Template): Record<string, unknown> {
  const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
  expect(runtimes).toHaveLength(1);
  return (runtimes[0].Properties?.EnvironmentVariables ?? {}) as Record<string, unknown>;
}
